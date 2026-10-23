"""Opt-in bounded real DeepSeek, independent-worker and lost-receipt recovery drill.

Only synthetic course data and a new random schema in a PostgreSQL *_test DB.
--offline exercises the same process/fault controller without any model HTTP.
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from time import monotonic
from uuid import UUID, uuid4

import httpx
from alembic import command
from alembic.config import Config
from dotenv import load_dotenv
from real_recovery_guard import SharedCallGuard
from real_recovery_worker import settings_for
from runtime_stress_support import REPO, DatabaseProxy, WorkerProcess, test_schema, test_url, until
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine
from teaching_assistant.api import create_app
from teaching_assistant.cli import bootstrap
from teaching_assistant.db import (
    AgentArtifact,
    AgentEvent,
    AgentModelReservation,
    AgentRun,
    Database,
)
from teaching_assistant.domain import digest
from teaching_assistant.harness import budgets
from verify_harness_real import (
    BRIEF,
    INSTRUCTIONS,
    MODEL,
    api_get,
    api_post,
    call_limit,
    capture,
    cost_limit,
    create_course,
    require,
    safe_error,
)

PARALLEL = (
    "这是合成并发恢复验收。先 read_lesson_brief，然后在同一轮调用 request_practice_designer "
    "和 request_review_agent。设计子任务须先读 brief、委派 practice 子任务，再原样保存其结果；"
    "review 子任务先读 brief，再保存简短检查建议。保留两个子任务的实际返回内容，"
    "最后 propose_parallel_lesson_plan 保存简短十分钟方案，等待教师确认。"
)
SIMPLE = "这是合成恢复验收。先 read_lesson_brief，再 propose_lesson_plan 保存简短十分钟方案，等待教师确认。"


class Drill:
    def __init__(self, directory, db, settings, schema, url, proxy, guard, offline, report):
        self.directory, self.db, self.settings, self.schema = directory, db, settings, schema
        self.url, self.proxy, self.guard, self.offline, self.report = (
            url,
            proxy,
            guard,
            offline,
            report,
        )
        self.processes, self.roots, self.gates = [], [], []
        self.control()

    def control(self):
        temporary = self.directory / "control.tmp"
        temporary.write_text(
            json.dumps({"roots": self.roots, "gates": self.gates}), encoding="utf-8"
        )
        temporary.replace(self.directory / "control.json")

    def persist(self):
        self.report["global_guard"] = self.guard.snapshot()
        (self.directory / "report.json").write_text(
            json.dumps(self.report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )

    async def start_worker(self):
        name = f"real-worker-{len(self.processes) + 1}"
        log = (self.directory / f"{name}.log").open("x", encoding="utf-8")
        stop = self.directory / f"{name}.stop"
        args = [
            sys.executable,
            str(REPO / "backend/tools/real_recovery_worker.py"),
            "--schema",
            self.schema,
            "--name",
            name,
            "--directory",
            str(self.directory),
            "--stop",
            str(stop),
        ]
        if self.offline:
            args.append("--offline")
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                cwd=REPO,
                env={
                    **os.environ,
                    "TA_REAL_DRILL_DATABASE_URL": self.url,
                    "PYTHONUNBUFFERED": "1",
                    "PYTHONIOENCODING": "utf-8",
                },
                stdout=log,
                stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except BaseException:
            log.close()
            raise
        worker = WorkerProcess(process, log, stop, name, "harness", None)
        self.processes.append(worker)
        return worker

    async def create(self, client, name, profile="lesson", *, max_calls=12):
        course_id = await create_course(client, name)
        alias = {
            "lesson": "lesson",
            "parallel": "lesson-parallel",
            "background": "background-lesson",
        }[profile]
        row = await api_post(
            client,
            f"/v1/courses/{course_id}/lesson-agent-runs",
            {
                "agent_profile": f"deepseek-{alias}-agent-v1",
                "brief": BRIEF,
                "instructions": PARALLEL
                if profile == "parallel"
                else INSTRUCTIONS["background"]
                if profile == "background"
                else SIMPLE,
                "max_model_calls": max_calls,
                "max_tool_calls": 24,
                "max_seconds": 900,
                "max_context_characters": 40000,
                "max_total_tokens": 300000,
                "max_cost_usd": "0.10",
            },
        )
        self.roots.append(row["id"])
        self.report["scenarios"].append(
            {"name": name, "run_id": row["id"], "expected_failure": max_calls == 2}
        )
        return row["id"]

    def add_gate(self, name, root_id, phase, **filters):
        self.gates.append({"name": name, "root_id": root_id, "phase": phase, **filters})

    async def gate(self, name):
        async def ready():
            self.check_workers()
            return self.guard.snapshot()["gates"].get(name)

        return await until(ready, deadline_seconds=180, label=name)

    def check_workers(self):
        for worker in self.processes:
            require(
                worker.process.returncode is None or worker.name in self.report["killed_workers"],
                "worker_exited_unexpectedly",
            )
        snapshot = self.guard.snapshot()
        require(not snapshot["denials"], "global_safety_gate_denied")

    async def kill_at(self, name):
        gate = await self.gate(name)
        worker = next(value for value in self.processes if value.name == gate["worker"])
        before = await capture(self.db, UUID(gate["run_id"]))
        record = {
            "name": name,
            **gate,
            "before": before,
            "calls_before": self.guard.snapshot()["calls_started"],
        }
        started = monotonic()
        record["exit_code"] = await worker.kill()
        self.report["killed_workers"].append(worker.name)
        self.report["faults"].append(record)
        self.persist()
        await self.start_worker()
        return record, started

    async def done(self, roots):
        async def complete():
            self.check_workers()
            async with self.db.sessions() as session:
                states = list(
                    await session.scalars(
                        select(AgentRun).where(
                            AgentRun.id.in_(tuple(UUID(value) for value in roots))
                        )
                    )
                )
            for row in states:
                expected_failure = next(
                    item["expected_failure"]
                    for item in self.report["scenarios"]
                    if item["run_id"] == str(row.id)
                )
                require(
                    row.status not in {"failed", "cancelled"} or expected_failure,
                    "unexpected_terminal_failure",
                )
            return len(states) == len(roots) and all(
                row.status in {"waiting_teacher", "failed"} for row in states
            )

        await until(complete, deadline_seconds=240, label="terminal_runs")
        for case in self.report["scenarios"]:
            if case["run_id"] in roots:
                case["evidence"] = await capture(self.db, UUID(case["run_id"]))
        self.persist()
        print(
            json.dumps({"completed": roots, "calls": self.guard.snapshot()["calls_started"]}),
            flush=True,
        )

    async def scenarios(self, client):
        parallel = await self.create(client, "concurrent_parallel_child_loss", "parallel")
        background = await self.create(client, "concurrent_background_join", "background")
        self.add_gate(
            "child_reply_lost", parallel, "response_received", task_type="lesson-practice-designer"
        )
        self.control()
        for _ in range(3):
            await self.start_worker()
        record, start = await self.kill_at("child_reply_lost")
        await self.done([parallel, background])
        record["recovered_after_seconds"] = monotonic() - start

        unknown = await self.create(client, "request_sent_process_kill")
        self.add_gate("request_lost", unknown, "request_sent")
        self.control()
        record, start = await self.kill_at("request_lost")
        await self.done([unknown])
        record["recovered_after_seconds"] = monotonic() - start

        committed = await self.create(client, "committed_reply_process_kill")
        self.add_gate("reply_committed", committed, "response_saved", tool="propose_lesson_plan")
        self.control()
        record, start = await self.kill_at("reply_committed")
        await self.done([committed])
        record["recovered_after_seconds"] = monotonic() - start
        require(
            self.guard.snapshot()["calls_started"] == record["calls_before"],
            "committed_reply_was_reinvoked",
        )

        outage = await self.create(client, "reply_then_database_disconnect")
        self.add_gate("database_cut", outage, "response_received")
        self.control()
        gate = await self.gate("database_cut")
        cut = self.proxy.disconnect()
        (self.directory / "database_cut.release").touch()
        await asyncio.sleep(2)
        self.proxy.restore()
        restored = monotonic()
        await self.done([outage])
        self.report["faults"].append(
            {
                "name": "database_cut",
                **gate,
                "cut_connections": cut,
                "outage_seconds": 2,
                "recovered_after_seconds": monotonic() - restored,
            }
        )

        exhausted = await self.create(client, "unknown_calls_exhaust_budget", max_calls=2)
        for index in (1, 2):
            self.add_gate(f"budget_unknown_{index}", exhausted, "request_sent")
        self.control()
        for index in (1, 2):
            await self.kill_at(f"budget_unknown_{index}")
        await self.done([exhausted])

    async def audit(self, client):
        async with self.db.sessions() as session:
            runs = list(await session.scalars(select(AgentRun)))
            events = list(
                await session.scalars(
                    select(AgentEvent).order_by(AgentEvent.run_id, AgentEvent.sequence)
                )
            )
            reservations = list(await session.scalars(select(AgentModelReservation)))
            artifacts = list(await session.scalars(select(AgentArtifact)))
            reports = {
                str(root.id): await budgets.report(session, root)
                for root in runs
                if root.parent_run_id is None
            }
        guard = self.guard.snapshot()
        receipts, by_run = defaultdict(list), defaultdict(list)
        for event in events:
            by_run[event.run_id].append(event)
            if event.kind in {"model_succeeded", "model_unknown", "model_failed"}:
                receipts[str(event.run_id), event.body["call_sequence"]].append(event)
        calls = {(item["run_id"], item["sequence"]): item for item in guard["calls"]}
        require(
            len(calls) == len(reservations) == guard["calls_started"], "dispatch_count_mismatch"
        )
        require(not guard["denials"], "guard_denied_calls")
        require(
            Decimal(guard["accounted_peak_cost_usd"]) <= Decimal(guard["max_cost_usd"]),
            "global_cost_overrun",
        )
        unknown = []
        for row in reservations:
            key = (str(row.run_id), row.dispatch_sequence)
            require(key in calls and len(receipts[key]) == 1, "unpaired_dispatch_receipt")
            call, receipt = calls[key], receipts[key][0]
            require(row.request_hash == call["request_hash"], "request_hash_mismatch")
            if receipt.kind == "model_unknown":
                require(row.settled_at is None and row.usage is None, "unknown_usage_was_released")
                unknown.append(key)
            else:
                require(
                    receipt.kind == "model_succeeded" and row.settled_at is not None,
                    "known_usage_not_settled",
                )
                require(row.usage == call["metadata"]["usage"], "usage_receipt_changed")
                require(call["metadata"].get("provider_request_id"), "missing_provider_request_id")
        children = [run for run in runs if run.parent_run_id]
        require(
            len({(row.parent_run_id, row.parent_tool_call_id) for row in children})
            == len(children)
            == 4,
            "duplicated_child",
        )
        expected_unknowns = {
            "concurrent_parallel_child_loss": 1,
            "concurrent_background_join": 0,
            "request_sent_process_kill": 1,
            "committed_reply_process_kill": 0,
            "reply_then_database_disconnect": 1,
            "unknown_calls_exhaust_budget": 2,
        }
        for run in runs:
            require(
                run.inflight_seq is run.lease_until is run.lease_owner is None
                and not run.pending_calls,
                "live_work_after_stop",
            )
            require(
                [event.sequence for event in by_run[run.id]] == list(range(1, run.event_seq + 1)),
                "event_gap_or_duplicate",
            )
            require(
                run.models_used
                == sum(event.kind == "model_dispatched" for event in by_run[run.id]),
                "model_counter_diverged",
            )
            requested = [
                call["id"] for message in run.messages for call in message.get("tool_calls", [])
            ]
            results = [
                message["tool_call_id"] for message in run.messages if message.get("role") == "tool"
            ]
            require(
                Counter(requested) == Counter(results)
                and all(value == 1 for value in Counter(results).values()),
                "duplicate_or_unpaired_tool",
            )
            if run.parent_run_id:
                require(run.status == "completed", "child_not_completed")
                continue
            case = next(case for case in self.report["scenarios"] if case["run_id"] == str(run.id))
            total = reports[str(run.id)]
            own_rows = [row for row in reservations if row.root_run_id == run.id]
            independently_accounted_tokens = sum(
                row.reserved_input_tokens + row.reserved_output_tokens
                if row.settled_at is None
                else row.usage["input_tokens"] + row.usage["output_tokens"]
                for row in own_rows
            )
            independently_accounted_cost = Decimal(0)
            for row in own_rows:
                if row.settled_at is None:
                    independently_accounted_cost += row.reserved_cost_usd
                    continue
                usage = row.usage
                hit = usage.get("input_cached_tokens", 0)
                cost = (
                    Decimal(usage["input_tokens"] - hit)
                    * Decimal(row.policy["input_usd_per_million"])
                    + Decimal(hit) * Decimal(row.policy["cached_input_usd_per_million"])
                    + Decimal(usage["output_tokens"])
                    * Decimal(row.policy["output_usd_per_million"])
                ) / Decimal(1000000)
                require(row.settled_cost_usd == cost, "settled_cost_mismatch")
                independently_accounted_cost += cost
            require(
                total["accounted_tokens"] == independently_accounted_tokens
                and Decimal(total["accounted_cost_usd"]) == independently_accounted_cost,
                "budget_report_diverged_from_receipts",
            )
            nodes = [row for row in runs if row.id == run.id or row.root_run_id == run.id]
            require(
                run.tree_models_used
                == sum(row.models_used for row in nodes)
                <= run.limits["max_model_calls"],
                "tree_model_budget_diverged",
            )
            require(
                run.tree_tools_used
                == sum(row.tools_used for row in nodes)
                <= run.limits["max_tool_calls"],
                "tree_tool_budget_diverged",
            )
            require(
                total["unsettled_calls"] == expected_unknowns[case["name"]],
                "unexpected_unknown_count",
            )
            require(
                total["accounted_tokens"] <= run.limits["max_total_tokens"]
                and Decimal(total["accounted_cost_usd"]) <= Decimal(run.limits["max_cost_usd"]),
                "tree_token_or_cost_limit_exceeded",
            )
            require(
                run.status == ("failed" if case["expected_failure"] else "waiting_teacher"),
                "wrong_terminal_state",
            )
            if case["expected_failure"]:
                require(
                    run.models_used == 2
                    and run.tools_used == 0
                    and run.last_error == "agent_model_budget_exhausted",
                    "unknown_budget_did_not_stop",
                )
            for suffix in ("budget", "children", "events?limit=100"):
                await api_get(client, f"/v1/agent-runs/{run.id}/{suffix}")
            case["status"] = "passed"
        require(
            len(artifacts) == len(runs) - 1 == len({row.run_id for row in artifacts}),
            "artifact_duplicated_or_missing",
        )
        require(
            all(
                row.confirmed_at is None and row.content_hash == digest(row.body)
                for row in artifacts
            ),
            "invalid_artifact",
        )
        # Overlapping send/receipt intervals prove network calls in separate processes,
        # not merely multiple rows left in running state after a crash.
        intervals = [
            (
                datetime.fromisoformat(item["sent_at"]),
                datetime.fromisoformat(item["ended_at"]),
                item,
            )
            for item in guard["calls"]
            if item.get("sent_at") and item.get("ended_at")
        ]
        overlap = [
            (a[2]["ordinal"], b[2]["ordinal"])
            for a in intervals
            for b in intervals
            if a[2]["ordinal"] < b[2]["ordinal"]
            and a[2]["worker"] != b[2]["worker"]
            and max(a[0], b[0]) < min(a[1], b[1])
        ]
        require(bool(overlap), "no_cross_process_model_overlap")
        child_ids = {str(row.id) for row in children}
        child_overlap = [
            pair
            for pair in overlap
            if all(guard["calls"][ordinal - 1]["run_id"] in child_ids for ordinal in pair)
        ]
        require(bool(child_overlap), "no_child_model_overlap")
        return {
            "runs": len(runs),
            "children": len(children),
            "artifacts": len(artifacts),
            "model_calls": len(calls),
            "unknown_runtime_calls": len(unknown),
            "unknown_guard_calls": sum(not item.get("usage_complete") for item in guard["calls"]),
            "cross_process_overlaps": overlap,
            "child_overlaps": child_overlap,
            "budgets": reports,
            "all_invariants_passed": True,
        }


async def verify(args):
    load_dotenv(REPO / ".env")
    url = os.environ["TA_TEST_DATABASE_URL"]
    parsed = test_url(url)
    settings = settings_for(url, offline=args.offline)
    require(bool(settings.deepseek_api_key), "deepseek_key_required")
    directory = args.output / str(uuid4())
    directory.mkdir(parents=True, exist_ok=False)
    guard = SharedCallGuard.create(
        directory / "calls.sqlite3",
        max_calls=args.max_calls,
        max_cost_usd=args.max_cost_usd,
        offline=args.offline,
    )
    schema = test_schema("runtime_stress_" + uuid4().hex)
    admin, db, created, drill = create_async_engine(url), None, False, None
    proxy = DatabaseProxy(parsed.host, parsed.port or 5432)
    report = {
        "passed": False,
        "offline": args.offline,
        "model": MODEL,
        "synthetic_inputs_only": True,
        "scenarios": [],
        "faults": [],
        "killed_workers": [],
        "cleanup": {},
    }
    started = monotonic()
    try:
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            created = True
        db = Database(url, schema=schema)
        async with db.engine.begin() as connection:

            def migrate(sync_connection):
                config = Config(str(REPO / "alembic.ini"))
                config.attributes["connection"] = sync_connection
                command.upgrade(config, "head")

            await connection.run_sync(migrate)
        await bootstrap(db, settings, name="Synthetic recovery teacher")
        port = await proxy.start()
        worker_url = parsed.set(host="127.0.0.1", port=port).render_as_string(hide_password=False)
        drill = Drill(
            directory, db, settings, schema, worker_url, proxy, guard, args.offline, report
        )
        app = create_app(settings, db)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://synthetic-recovery",
            headers={"Authorization": f"Bearer {settings.dev_api_token}"},
        ) as client:
            async with asyncio.timeout(900):
                await drill.scenarios(client)
                report["audit"] = await drill.audit(client)
        report["passed"] = True
    except (Exception, asyncio.CancelledError) as exc:
        report["error"] = safe_error(exc)
    finally:
        proxy.restore()

        async def cleanup_step(name, operation):
            try:
                await operation()
                report["cleanup"][name] = True
            except Exception as exc:
                report["cleanup"][name] = safe_error(exc)
                report["passed"] = False

        if drill is not None:
            for worker in drill.processes:
                await cleanup_step(worker.name, worker.stop)
            report["cleanup"]["workers_closed"] = all(
                worker.process.returncode is not None for worker in drill.processes
            )
            for case in report["scenarios"]:
                try:
                    case["evidence"] = await capture(db, UUID(case["run_id"]))
                except Exception as exc:
                    report["capture_error"] = safe_error(exc)
                    report["passed"] = False
            report["elapsed_seconds"] = monotonic() - started
            try:
                drill.persist()
            except OSError:
                report["error"] = "report_write_failed"
                report["passed"] = False
        await cleanup_step("proxy_closed", proxy.close)
        if db is not None:
            await cleanup_step("database_closed", db.close)
        if created:

            async def drop_schema():
                async with admin.begin() as connection:
                    await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))

            await cleanup_step("schema_dropped", drop_schema)
        await cleanup_step("admin_closed", admin.dispose)
        report["global_guard"] = guard.snapshot()
        (directory / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
    print(
        json.dumps(
            {
                "passed": report["passed"],
                "offline": args.offline,
                "error": report.get("error"),
                "directory": str(directory),
                "calls": guard.snapshot()["calls_started"],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-calls", type=call_limit, default=36)
    parser.add_argument("--max-cost-usd", type=cost_limit, default=Decimal("0.30"))
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--output", type=Path, default=REPO / ".local/e2e/harness-recovery-real")
    try:
        raise SystemExit(asyncio.run(verify(parser.parse_args())))
    except (Exception, KeyboardInterrupt) as exc:
        print(json.dumps({"passed": False, "error": safe_error(exc)}))
        raise SystemExit(1) from None

"""Sustained multi-process recovery drill using a random *_test schema and fake models."""

import argparse
import asyncio
import json
import os
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic, perf_counter
from uuid import UUID, uuid4

import httpx
from alembic import command
from alembic.config import Config
from dotenv import load_dotenv
from runtime_stress_support import REPO, DatabaseProxy, WorkerProcess, settings_for, test_url, until
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import create_async_engine
from teaching_assistant.api import create_app
from teaching_assistant.cli import bootstrap
from teaching_assistant.db import (
    AgentArtifact,
    AgentEvent,
    AgentModelReservation,
    AgentRun,
    Assessment,
    Attempt,
    Database,
    GradingBatch,
    GradingItem,
    ModelCall,
    Step,
)
from teaching_assistant.demo import DEMO_RUBRIC
from teaching_assistant.domain import digest


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def percentiles(values):
    ordered = sorted(values)
    if not ordered:
        return {}
    return {
        name: round(ordered[min(len(ordered) - 1, int((len(ordered) - 1) * q))], 3)
        for name, q in (("p50", 0.50), ("p95", 0.95), ("p99", 0.99), ("max", 1))
    }


class Drill:
    def __init__(self, args, db, client, proxy, proxy_url, schema):
        self.args, self.db, self.client = args, db, client
        self.proxy, self.url, self.schema = proxy, proxy_url, schema
        self.processes, self.current = [], {}
        self.faults, self.samples, self.api_ms = [], [], []
        self.sequence = self.roots_created = self.items_created = 0
        self.monitor_stop = asyncio.Event()

    async def post(self, path, body):
        started = perf_counter()
        response = await self.client.post(path, json=body, headers={"Idempotency-Key": uuid4().hex})
        self.api_ms.append((perf_counter() - started) * 1000)
        response.raise_for_status()
        return response.json()

    async def setup(self):
        self.course = await self.post(
            "/v1/workspaces",
            {
                "course_name": "Synthetic runtime stress",
                "classroom_name": "Recovery fixture",
                "students": [
                    {"external_id": f"stress-{i}", "name": f"Synthetic {i}"} for i in range(10)
                ],
            },
        )
        self.assignment = await self.post(
            f"/v1/courses/{self.course['course_id']}/assignments",
            {
                "classroom_id": self.course["classroom_id"],
                "title": "Synthetic recovery drill",
            },
        )
        self.question = await self.post(
            f"/v1/assignments/{self.assignment['id']}/question-versions",
            {
                "question_key": "versioning",
                "prompt": "Explain version history.",
                "reference": "Preserve immutable versions.",
                "maximum_points": "10.00",
            },
        )
        draft = await self.post(
            f"/v1/question-versions/{self.question['id']}/rubric-drafts",
            {
                "rubric": DEMO_RUBRIC,
            },
        )
        self.rubric = await self.post(
            f"/v1/rubric-drafts/{draft['id']}/confirm", {"expected_revision": 1}
        )

    async def root(self, profile="fake-lesson-agent-v1"):
        result = await self.post(
            f"/v1/courses/{self.course['course_id']}/lesson-agent-runs",
            {
                "agent_profile": profile,
                "brief": {
                    "topic": "Recovery under concurrency",
                    "objectives": ["Preserve durable work"],
                    "duration_minutes": 10,
                },
                "max_seconds": 300,
                "max_model_calls": 12,
                "max_tool_calls": 24,
            },
        )
        self.roots_created += 1
        return UUID(result["id"])

    async def batch(self, size=10):
        revision = uuid4().hex
        imported = await self.post(
            f"/v1/assignments/{self.assignment['id']}/imports",
            {
                "rows": [
                    {
                        "student_id": student["id"],
                        "question_version_id": self.question["id"],
                        "external_submission_id": f"stress-{i}",
                        "source_revision": revision,
                        "answer_text": "Synthetic answer: versions preserve history.",
                    }
                    for i, student in enumerate(self.course["students"][:size])
                ]
            },
        )
        batch = await self.post(
            "/v1/grading-batches",
            {
                "rubric_version_id": self.rubric["id"],
                "answer_version_ids": imported["answer_version_ids"],
            },
        )
        self.items_created += size
        return [UUID(value) for value in batch["item_ids"]]

    async def start(self, name, kind, *, gate=None, slots=1):
        self.sequence += 1
        worker = await WorkerProcess.start(
            self.args.output,
            self.url,
            self.schema,
            f"{name}-{self.sequence}",
            kind,
            slots=slots,
            gate=gate,
        )
        self.processes.append(worker)
        return worker

    async def finished(self, kind, target):
        table = AgentRun if kind == "harness" else GradingItem
        async with self.db.sessions() as session:
            row = await session.get(table, target)
            if row.status in {"failed", "cancelled"}:
                raise AssertionError(f"{kind} failed: {row.last_error}")
            return row if row.status in {"waiting_teacher", "completed", "succeeded"} else None

    async def gated_crash(self, gate):
        kind = "grading" if gate == "grading_evidence" else "harness"
        target = (
            (await self.batch(1))[0]
            if kind == "grading"
            else await self.root(
                "fake-lesson-parallel-agent-v1"
                if gate == "designer_model"
                else "fake-lesson-agent-v1"
            )
        )
        root_id = target
        worker = await self.start(gate, kind, gate=gate)

        async def signalled():
            require(worker.process.returncode is None, "Barrier worker exited before injection")
            if worker.signal.exists():
                try:
                    return json.loads(worker.signal.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    return None
            return None

        signal = await until(signalled, label=gate)
        if gate == "designer_model":
            target = UUID(signal["run_id"])
        async with self.db.sessions() as session:
            table = AgentRun if kind == "harness" else GradingItem
            before = await session.get(table, target)
            require(before.status == "running", "Crash barrier must hold active work")
            epoch = before.epoch
            if gate == "tool_commit":
                require(
                    before.models_used == 2 and len(before.pending_calls) == 1,
                    "Pending tool not saved",
                )
                require(
                    await session.scalar(
                        select(func.count())
                        .select_from(AgentArtifact)
                        .where(AgentArtifact.run_id == target)
                    )
                    == 0,
                    "Uncommitted tool artifact leaked before process kill",
                )
            checkpoint = None
            if kind == "grading":
                checkpoint = await session.scalar(select(Step.id).where(Step.item_id == target))
                require(
                    checkpoint is not None and before.calls_used == 2,
                    "Evaluation checkpoint missing",
                )
        started = monotonic()
        exit_code = await worker.kill()
        replacement = await self.start(gate + "-replacement", kind)
        after = await until(lambda: self.finished(kind, target), label=gate + " recovery")
        if gate == "designer_model":
            parent = await until(lambda: self.finished(kind, root_id), label="nested root recovery")
            require(parent.tree_models_used == 11, "Nested recovery lost or duplicated usage")
        elapsed = monotonic() - started
        await replacement.stop()
        require(
            after.epoch > epoch and after.attempts == 2, "Killed lease did not recover exactly once"
        )
        async with self.db.sessions() as session:
            if kind == "harness":
                unknown = await session.scalar(
                    select(func.count())
                    .select_from(AgentEvent)
                    .where(AgentEvent.run_id == target, AgentEvent.kind == "model_unknown")
                )
                require(
                    unknown == (1 if gate in {"model", "designer_model"} else 0),
                    "Unknown model receipt mismatch",
                )
                require(
                    after.models_used == {"model": 3, "designer_model": 4, "tool_commit": 2}[gate],
                    "Saved model work was replayed",
                )
                require(
                    await session.scalar(
                        select(func.count())
                        .select_from(AgentArtifact)
                        .where(AgentArtifact.run_id == target)
                    )
                    == 1,
                    "Expected one recovered artifact",
                )
            else:
                require(
                    await session.get(Step, checkpoint) is not None, "Saved evaluation was replaced"
                )
                require(after.calls_used == 5, "Successful evaluation was repeated after crash")
                unknown = await session.scalar(
                    select(func.count())
                    .select_from(ModelCall)
                    .where(ModelCall.item_id == target, ModelCall.status == "unknown")
                )
                require(unknown == 1, "Expected one unknown evidence request")
        result = {
            "kind": "process_kill",
            "barrier": gate,
            "exit_code": exit_code,
            "recovery_seconds": round(elapsed, 3),
            "unknown_calls": unknown,
            "checkpoint_reused": True,
            "duplicate_effects": 0,
        }
        self.faults.append(result)
        print(json.dumps(result), flush=True)

    async def counts(self):
        async with self.db.sessions() as session:
            agents = dict(
                (
                    await session.execute(
                        select(AgentRun.status, func.count()).group_by(AgentRun.status)
                    )
                ).all()
            )
            grading = dict(
                (
                    await session.execute(
                        select(GradingItem.status, func.count()).group_by(GradingItem.status)
                    )
                ).all()
            )
            inflight = await session.scalar(
                select(func.count()).select_from(AgentRun).where(AgentRun.inflight_seq.is_not(None))
            )
            live_children = list(
                await session.scalars(
                    select(func.count())
                    .select_from(AgentRun)
                    .where(
                        AgentRun.parent_run_id.is_not(None),
                        AgentRun.status == "running",
                        AgentRun.lease_until > func.clock_timestamp(),
                    )
                    .group_by(AgentRun.root_run_id)
                )
            )
        maximum = max(live_children, default=0)
        require(maximum <= 2, "A task tree exceeded its child concurrency limit")
        return {
            "harness": agents,
            "grading": grading,
            "inflight_models": inflight,
            "max_live_children_per_tree": maximum,
        }

    async def monitor(self, started):
        tick = 0
        while not self.monitor_stop.is_set():
            for worker in self.current.values():
                require(worker.process.returncode is None, f"Unexpected worker exit: {worker.name}")
            sample = {"second": round(monotonic() - started, 2), **await self.counts()}
            self.samples.append(sample)
            if tick % 15 == 0:
                print(json.dumps({"progress": sample}), flush=True)
                (self.args.output / "progress.json").write_text(
                    json.dumps(sample), encoding="utf-8"
                )
            tick += 1
            try:
                await asyncio.wait_for(self.monitor_stop.wait(), timeout=1)
            except TimeoutError:
                pass

    async def produce(self, started):
        profiles = (
            "fake-lesson-agent-v1",
            "fake-lesson-parallel-agent-v1",
            "fake-background-lesson-agent-v1",
            "fake-lesson-agent-v1",
        )
        index = 0
        while monotonic() - started < self.args.seconds:
            await self.root(profiles[index % len(profiles)])
            if index % 20 == 0:
                await self.batch()
            index += 1
            await asyncio.sleep(max(0, started + index / self.args.rate - monotonic()))

    async def kill_busy(self, key):
        worker = self.current[key]
        kind = worker.kind

        async def busy():
            async with self.db.sessions() as session:
                if kind == "harness":
                    return list(
                        await session.scalars(
                            select(AgentRun.id).where(
                                AgentRun.lease_owner.startswith(worker.name + "-"),
                                AgentRun.inflight_seq.is_not(None),
                            )
                        )
                    )
                return list(
                    await session.scalars(
                        select(GradingItem.id).where(
                            GradingItem.lease_owner == worker.name, GradingItem.status == "running"
                        )
                    )
                )

        targets = await until(busy, label="busy worker for kill")
        started = monotonic()
        # Remove the intentionally killed process from liveness monitoring first.
        self.current.pop(key)
        code = await worker.kill()
        self.current[key] = await self.start(
            key, kind, slots=self.args.slots if kind == "harness" else 1
        )
        for target in targets:
            await until(
                lambda target=target: self.finished(kind, target),
                deadline_seconds=60,
                label="load kill recovery",
            )
        self.faults.append(
            {
                "kind": "process_kill_under_load",
                "worker_kind": kind,
                "affected_tasks": len(targets),
                "exit_code": code,
                "recovery_seconds": round(monotonic() - started, 3),
            }
        )
        print(json.dumps(self.faults[-1]), flush=True)

    async def outage(self, seconds):
        # Cut active work, not only idle pool connections. If the grading burst
        # has drained, add a small probe batch using the same production API.
        state = await self.counts()
        if not state["grading"].get("running"):
            await self.batch(3)

        async def active_work():
            state = await self.counts()
            return state if state["inflight_models"] and state["grading"].get("running") else None

        before = await until(active_work, label="active work before database disconnect")
        async with self.db.sessions() as session:
            agents = list(
                await session.scalars(
                    select(AgentRun.id).where(
                        AgentRun.status.in_(("queued", "running", "waiting_children"))
                    )
                )
            )
            grading = list(
                await session.scalars(select(GradingItem.id).where(GradingItem.status == "running"))
            )
        started = monotonic()
        rejected = self.proxy.rejected
        cut = self.proxy.disconnect()
        try:
            await asyncio.sleep(seconds)
        finally:
            self.proxy.restore()
        restored = monotonic()
        for kind, targets in (("harness", agents), ("grading", grading)):
            for target in targets:
                await until(
                    lambda kind=kind, target=target: self.finished(kind, target),
                    deadline_seconds=60,
                    label="database reconnect recovery",
                )
        self.faults.append(
            {
                "kind": "database_disconnect",
                "duration_seconds": round(restored - started, 3),
                "connections_cut": cut,
                "connections_rejected": self.proxy.rejected - rejected,
                "affected_harness_tasks": len(agents),
                "affected_grading_tasks": len(grading),
                "recovery_after_reconnect_seconds": round(monotonic() - restored, 3),
                "before": before,
            }
        )
        require(cut > 0, "Database fault did not cut a worker connection")
        print(json.dumps(self.faults[-1]), flush=True)

    async def inject(self, started):
        schedule = ((0.20, "h0"), (0.40, "database"), (0.65, "g0"), (0.80, "database"))
        for fraction, action in schedule:
            await asyncio.sleep(max(0, started + self.args.seconds * fraction - monotonic()))
            if action == "database":
                await self.outage(self.args.outage_seconds)
            else:
                await self.kill_busy(action)

    async def run_load(self):
        for index in range(self.args.workers):
            key = f"h{index}"
            self.current[key] = await self.start(key, "harness", slots=self.args.slots)
        for index in range(3):
            key = f"g{index}"
            self.current[key] = await self.start(key, "grading")
        started = monotonic()
        monitor = asyncio.create_task(self.monitor(started))
        producer = asyncio.create_task(self.produce(started))
        faults = asyncio.create_task(self.inject(started))
        group = asyncio.gather(producer, faults)
        try:
            # A monitor failure must also stop the producer/fault controller.
            done, _ = await asyncio.wait((group, monitor), return_when=asyncio.FIRST_COMPLETED)
            if monitor in done:
                await monitor
            await group

            async def drained():
                state = await self.counts()
                require(
                    not state["harness"].get("failed") and not state["grading"].get("failed"),
                    "A workload task failed; inspect final audit",
                )
                return all(
                    status in {"waiting_teacher", "completed"} for status in state["harness"]
                ) and all(status == "succeeded" for status in state["grading"])

            await until(drained, deadline_seconds=120, label="workload drain")
        finally:
            self.monitor_stop.set()
            for task in (producer, faults, monitor):
                task.cancel()
            await asyncio.gather(producer, faults, monitor, return_exceptions=True)
            await asyncio.gather(group, return_exceptions=True)
        return round(monotonic() - started, 3)

    async def audit(self):
        async with self.db.sessions() as session:
            runs = list(await session.scalars(select(AgentRun)))
            events = list(
                await session.scalars(
                    select(AgentEvent).order_by(AgentEvent.run_id, AgentEvent.sequence)
                )
            )
            artifacts = list(await session.scalars(select(AgentArtifact)))
            reservations = list(await session.scalars(select(AgentModelReservation)))
            items = list(await session.scalars(select(GradingItem)))
            calls = list(await session.scalars(select(ModelCall)))
            steps = list(await session.scalars(select(Step)))
            attempts = list(await session.scalars(select(Attempt)))
            assessments = list(await session.scalars(select(Assessment)))
            batches = list(await session.scalars(select(GradingBatch)))
        by_run, tree, receipts = defaultdict(list), defaultdict(list), defaultdict(list)
        for event in events:
            by_run[event.run_id].append(event)
            if event.kind in {"model_succeeded", "model_unknown", "model_failed"}:
                receipts[event.run_id, event.body["call_sequence"]].append(event)
        for run in runs:
            tree[run.root_run_id or run.id].append(run)
            require(
                run.status == ("completed" if run.parent_run_id else "waiting_teacher"),
                "Non-successful final Harness status",
            )
            require(
                run.lease_owner is run.lease_until is run.inflight_seq is None
                and not run.pending_calls,
                "Terminal Harness task retains live work",
            )
            require(
                [event.sequence for event in by_run[run.id]] == list(range(1, run.event_seq + 1)),
                "Event sequence has a gap",
            )
            dispatches = [event for event in by_run[run.id] if event.kind == "model_dispatched"]
            require(len(dispatches) == run.models_used, "Model budget diverged from dispatches")
            require(
                all(len(receipts[run.id, event.sequence]) == 1 for event in dispatches),
                "Model dispatch lacks one terminal receipt",
            )
            requested = [call["id"] for msg in run.messages for call in msg.get("tool_calls", [])]
            results = [msg["tool_call_id"] for msg in run.messages if msg.get("role") == "tool"]
            require(
                Counter(requested) == Counter(results)
                and all(v == 1 for v in Counter(results).values()),
                "Unpaired or duplicate tool result",
            )
        for root_id, nodes in tree.items():
            root = next(run for run in nodes if run.id == root_id)
            require(
                root.tree_models_used == sum(run.models_used for run in nodes),
                "Tree model accounting mismatch",
            )
            require(
                root.tree_tools_used == sum(run.tools_used for run in nodes),
                "Tree tool accounting mismatch",
            )
            require(
                root.tree_models_used <= root.limits["max_model_calls"]
                and root.tree_tools_used <= root.limits["max_tool_calls"],
                "Tree budget exceeded",
            )
        require(
            len(artifacts) == len(runs) == len({row.run_id for row in artifacts}),
            "Missing or duplicate artifact",
        )
        require(
            all(
                row.content_hash == digest(row.body) and row.confirmed_at is None
                for row in artifacts
            ),
            "Artifact changed or auto-confirmed",
        )
        require(
            len(reservations) == sum(run.models_used for run in runs), "Reservation count mismatch"
        )
        for row in reservations:
            receipt = receipts[row.run_id, row.dispatch_sequence][0]
            require(
                (row.settled_at is None) == (receipt.kind == "model_unknown"),
                "Unknown budget was released or known usage lost",
            )
        require(
            all(
                row.status == "succeeded" and row.lease_until is row.lease_owner is None
                for row in items
            ),
            "Grading work not fully recovered",
        )
        require(
            len(steps) == len(items) * 4 and len(assessments) == len(items) * 2,
            "Missing grading checkpoints or assessments",
        )
        require(
            all(row.status in {"succeeded", "unknown"} for row in calls), "Unclosed grading call"
        )
        require(
            all(row.status in {"succeeded", "abandoned"} for row in attempts),
            "Unclosed grading attempt",
        )
        require(
            all(row.status == "settled" and row.outcome == "all_succeeded" for row in batches),
            "Batch aggregation is stale",
        )
        require(
            sum(item.calls_used for item in items) == len(calls), "Grading call accounting mismatch"
        )
        steps_per_item = Counter(row.item_id for row in steps)
        assessments_per_item = Counter(row.item_id for row in assessments)
        calls_per_item = Counter(row.item_id for row in calls)
        for item in items:
            require(steps_per_item[item.id] == 4, "Grading item checkpoint count mismatch")
            require(assessments_per_item[item.id] == 2, "Grading item assessment count mismatch")
            require(calls_per_item[item.id] == item.calls_used, "Grading item budget mismatch")
        latencies, claim_delays = [], []
        for run in runs:
            if not run.parent_run_id:
                end = next(
                    event.created_at for event in by_run[run.id] if event.kind == "waiting_teacher"
                )
                first = next(
                    event.created_at for event in by_run[run.id] if event.kind == "claimed"
                )
                latencies.append((end - run.created_at).total_seconds())
                claim_delays.append((first - run.created_at).total_seconds())
        return {
            "roots": self.roots_created,
            "harness_runs": len(runs),
            "artifacts": len(artifacts),
            "grading_items": len(items),
            "grading_steps": len(steps),
            "assessments": len(assessments),
            "harness_model_calls": len(reservations),
            "unknown_harness_calls": sum(row.settled_at is None for row in reservations),
            "grading_model_calls": len(calls),
            "unknown_grading_calls": sum(row.status == "unknown" for row in calls),
            "root_completion_seconds": percentiles(latencies),
            "first_claim_seconds": percentiles(claim_delays),
            "api_milliseconds": percentiles(self.api_ms),
            "invariants_passed": True,
        }

    async def close(self):
        await asyncio.gather(*(worker.stop() for worker in self.processes))

    async def failure_snapshot(self):
        async with self.db.sessions() as session:
            rows = list(
                await session.scalars(
                    select(AgentRun).where(AgentRun.status.in_(("failed", "cancelled"))).limit(20)
                )
            )
            return [
                {
                    "task_type": row.task_type,
                    "status": row.status,
                    "error_code": row.last_error,
                    "models_used": row.models_used,
                    "max_model_calls": row.limits["max_model_calls"],
                    "tree_models_used": row.tree_models_used,
                    "attempts": row.attempts,
                }
                for row in rows
            ]


async def verify(args):
    load_dotenv(REPO / ".env")
    url = test_url(os.environ["TA_TEST_DATABASE_URL"])
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    schema = "runtime_stress_" + uuid4().hex
    raw_url = url.render_as_string(hide_password=False)
    admin = create_async_engine(raw_url)
    db = Database(raw_url, schema=schema)
    settings = settings_for(raw_url)
    proxy = DatabaseProxy(url.host or "127.0.0.1", url.port or 5432)
    port = await proxy.start()
    proxy_url = url.set(host="127.0.0.1", port=port).render_as_string(hide_password=False)
    report = {
        "started_at": datetime.now(UTC).isoformat(),
        "synthetic": True,
        "real_model_calls": 0,
        "configuration": {
            "seconds": args.seconds,
            "roots_per_second": args.rate,
            "harness_processes": args.workers,
            "slots_per_process": args.slots,
            "grading_processes": 3,
            "lease_seconds": 5,
            "outage_seconds": args.outage_seconds,
        },
        "passed": False,
        "schema_cleaned": False,
    }
    created = False
    drill = None
    try:
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            created = True
        async with db.engine.begin() as connection:

            def migrate(sync_connection):
                config = Config(str(REPO / "alembic.ini"))
                config.attributes["connection"] = sync_connection
                command.upgrade(config, "head")

            await connection.run_sync(migrate)
        await bootstrap(db, settings)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(settings, db)),
            base_url="http://stress",
            headers={"Authorization": f"Bearer {settings.dev_api_token}"},
        ) as client:
            drill = Drill(args, db, client, proxy, proxy_url, schema)
            await drill.setup()
            for gate in ("model", "designer_model", "tool_commit", "grading_evidence"):
                await drill.gated_crash(gate)
            report["workload_elapsed_seconds"] = await drill.run_load()
            report["audit"] = await drill.audit()
            report["passed"] = True
    except BaseException as exc:
        report["error"] = {
            "type": type(exc).__name__,
            "message": str(exc)
            if isinstance(exc, (AssertionError, RuntimeError))
            else "Drill failed; see stage evidence",
        }
        if drill is not None:
            try:
                report["failed_tasks"] = await asyncio.wait_for(drill.failure_snapshot(), 5)
            except Exception:
                report["failure_snapshot_unavailable"] = True
        raise
    finally:
        if drill is not None:
            await drill.close()
            report["faults"], report["samples"] = drill.faults, drill.samples
            report["worker_logs"] = [
                worker.log.name.split(os.sep)[-1] for worker in drill.processes
            ]
            logs = await asyncio.gather(
                *(asyncio.to_thread(Path(worker.log.name).read_bytes) for worker in drill.processes)
            )
            report["log_metrics"] = {
                "bytes": sum(len(log) for log in logs),
                "lines": sum(log.count(b"\n") for log in logs),
                "harness_claim_errors": sum(log.count(b"agent_pool_claim_error") for log in logs),
                "grading_errors": sum(log.count(b"worker_iteration_failed") for log in logs),
                "tracebacks": sum(log.count(b"Traceback (most recent call last)") for log in logs),
            }
            report["processes_stopped"] = all(
                worker.process.returncode is not None for worker in drill.processes
            )
        report["network"] = {
            "accepted": proxy.accepted,
            "rejected_during_outage": proxy.rejected,
            "connections_cut": proxy.cut_connections,
        }
        await proxy.close()
        await db.close()
        if created:
            async with admin.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            report["schema_cleaned"] = True
        await admin.dispose()
        report["finished_at"] = datetime.now(UTC).isoformat()
        (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(
            json.dumps(
                {
                    key: value
                    for key, value in report.items()
                    if key not in {"samples", "worker_logs"}
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=300)
    parser.add_argument("--rate", type=float, default=2)
    parser.add_argument("--workers", type=int, choices=range(2, 5), default=3)
    parser.add_argument("--slots", type=int, choices=range(1, 9), default=4)
    parser.add_argument("--outage-seconds", type=float, default=6)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if (
        not 20 <= args.seconds <= 3600
        or not 0.1 <= args.rate <= 10
        or args.seconds * args.rate > 5000
    ):
        parser.error("Use 20–3600 seconds, 0.1–10 roots/second, and at most 5000 roots")
    if not 0.5 <= args.outage_seconds <= 15:
        parser.error("Database outages must last 0.5–15 seconds")
    asyncio.run(verify(args))

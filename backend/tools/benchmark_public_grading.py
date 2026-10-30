"""Run pinned public holdouts through real Agent tools, Jev and isolated PostgreSQL.

Gold stays in offline metadata. Each wave is persisted before database cleanup;
completed waves can be resumed without repeating paid requests. A durable request
ledger is reserved before dispatch and never refunds failures or unknown calls.
"""

import argparse
import asyncio
import json
import os
from collections import Counter, defaultdict, deque
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
from alembic import command
from alembic.config import Config
from benchmark_jev_agent import REPO, audit, checked_url, execute, percentiles, seed
from dotenv import load_dotenv
from public_grading_data import build_suite
from public_grading_metrics import summarize_public_grading
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine
from teaching_assistant.api import create_app
from teaching_assistant.cli import bootstrap
from teaching_assistant.db import (
    AgentEvent,
    AgentModelReservation,
    AgentRun,
    Assessment,
    Attempt,
    Database,
    GradingItem,
    ModelCall,
    Step,
)
from teaching_assistant.domain import ModelError, digest, text_digest
from teaching_assistant.profiles import configured_jev_profile
from teaching_assistant.settings import Settings


def save_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


class DurableBudget:
    def __init__(self, path, maximum):
        self.path, self.maximum = path, maximum
        self.poisoned = False
        entries = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        for index, line in enumerate(entries, 1):
            if json.loads(line)["sequence"] != index:
                raise ValueError("Request ledger sequence is corrupt")
        self.used = len(entries)
        if self.used > self.maximum:
            raise ValueError("Configured budget is below existing reservations")

    def reserve(self):
        if self.poisoned:
            raise ModelError("benchmark_request_ledger_unavailable", retryable=False)
        if self.used >= self.maximum:
            raise ModelError("benchmark_call_budget_exhausted", retryable=False)
        # Charge before touching disk: append, flush or fsync may succeed without
        # returning an acknowledgement. This instance must never reuse its slot.
        self.used += 1
        entry = {"sequence": self.used, "reserved_at": datetime.now(UTC).isoformat()}
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException as error:
            self.poisoned = True
            if isinstance(error, Exception):
                raise ModelError("benchmark_request_ledger_unavailable", retryable=False) from None
            raise


class OutputLock:
    """An OS lock prevents concurrent writers and is released after process death."""

    def __init__(self, output):
        output.mkdir(parents=True, exist_ok=True)
        self.handle = (output / "runner.lock").open("a+b")

    def __enter__(self):
        if self.handle.seek(0, 2) == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            raise ValueError("Another runner owns this output directory") from None
        return self

    def __exit__(self, *_):
        self.handle.close()


def band_consistent(criteria, selected):
    incomplete = False
    for criterion in criteria:
        level = selected.get(criterion.id)
        if level is None:
            return None
        points = next(value.points for value in criterion.levels if value.id == level)
        if incomplete and points != 0:
            return False
        incomplete |= points < criterion.maximum_points
    return True


def select_samples(suite, count, policy):
    """A fixed, prediction-independent subset for repeated concurrency experiments."""
    if not count:
        return suite
    if policy == "prefix":
        candidates = [row["sample_id"] for group in suite for row in group["rows"]]
        chosen = set(candidates[:count])
    else:
        buckets = defaultdict(lambda: defaultdict(deque))
        for group in suite:
            for row in group["rows"]:
                buckets[row["dataset"]][(row["subject"], row["split"])].append(row["sample_id"])
        dataset_queues = {}
        for dataset, strata in sorted(buckets.items()):
            queues = deque(strata[key] for key in sorted(strata))
            values = deque()
            while queues:
                queue = queues.popleft()
                values.append(queue.popleft())
                if queue:
                    queues.append(queue)
            dataset_queues[dataset] = values
        queues = deque(dataset_queues.values())
        chosen = set()
        while queues and len(chosen) < count:
            queue = queues.popleft()
            chosen.add(queue.popleft())
            if queue:
                queues.append(queue)
    return select_ids(suite, chosen)


def select_ids(suite, chosen):
    available = {row["sample_id"] for group in suite for row in group["rows"]}
    if not chosen or not set(chosen) <= available:
        raise ValueError("Selected sample IDs must be a nonempty subset of the pinned source")
    result = []
    for group in suite:
        rows = [row for row in group["rows"] if row["sample_id"] in chosen]
        if rows:
            result.append(
                {
                    "dataset": group["dataset"].model_copy(
                        update={
                            "samples": [
                                sample
                                for sample in group["dataset"].samples
                                if sample.sample_id in chosen
                            ],
                        }
                    ),
                    "rows": rows,
                }
            )
    return result


def model_failure_receipt(event):
    """Export only typed diagnostic fields, never provider text or request bodies."""
    receipt = {"kind": event.kind, "event_sequence": event.sequence}
    body = event.body or {}
    code = body.get("error_code")
    if isinstance(code, str):
        receipt["error_code"] = (
            code
            if 0 < len(code) <= 120 and code.replace("_", "").replace("-", "").isalnum()
            else "invalid_error_code"
        )
    for field in ("call_sequence", "http_status"):
        value = body.get(field)
        if isinstance(value, int) and not isinstance(value, bool):
            if (field == "http_status" and 100 <= value <= 599) or (
                field == "call_sequence" and value >= 0
            ):
                receipt[field] = value
    usage = body.get("usage")
    if isinstance(usage, dict):
        receipt["usage"] = {
            key: value
            for key, value in usage.items()
            if key in {"input_tokens", "output_tokens", "cached_input_tokens"}
            and isinstance(value, int)
            and not isinstance(value, bool)
            and value >= 0
        }
    return receipt


def wave_blocker(wave):
    """Stop subsequent waves after account, ledger or total controller failure."""
    controller = wave.get("controller_usage") or {}
    codes = Counter(controller.get("error_codes") or {})
    codes.update(controller.get("terminal_error_codes") or {})
    http_statuses = Counter(controller.get("failed_http_statuses") or {})
    rows = wave.get("observations") or []
    for row in rows:
        if row.get("agent_error_code"):
            codes[row["agent_error_code"]] += 1
        for failure in row.get("agent_model_failures", []):
            if failure.get("error_code"):
                codes[failure["error_code"]] += 1
            if failure.get("http_status") is not None:
                http_statuses[str(failure["http_status"])] += 1
        for call in row.get("calls", []):
            if call.get("error_code"):
                codes[call["error_code"]] += 1
            if call.get("http_status") is not None and call.get("status") != "succeeded":
                http_statuses[str(call["http_status"])] += 1
    for status in (402, 401, 403):
        if http_statuses[str(status)] or http_statuses[status]:
            return f"provider_http_{status}"
    for code in ("provider_auth_error", "provider_insufficient_balance"):
        if codes[code]:
            return code
    for code in sorted(codes):
        if str(code).startswith("benchmark_") and codes[code]:
            return code
    no_grading_calls = not any(
        call.get("kind") == "evaluate" for row in rows for call in row.get("calls", [])
    )
    no_reservations = wave.get("request_end", 0) == wave.get("request_start", 0)
    if (
        rows
        and all(row.get("agent_status") == "failed" for row in rows)
        and no_grading_calls
        and no_reservations
    ):
        return "all_controllers_failed"
    return None


def wave_completion(wave):
    """Keep visiting a target separate from completing its grading workflow."""
    rows = wave.get("observations") or []
    expected = wave["expected_samples"]
    visited = len(rows)
    unique = len({row["sample_id"] for row in rows})
    visited_complete = expected > 0 and visited == unique == expected
    terminal = {"succeeded", "abstained", "failed"}
    graded = sum(row.get("status") in terminal for row in rows)
    grading_complete = visited_complete and graded == expected
    controllers_complete = visited_complete and all(
        row.get("agent_status") == "completed" for row in rows
    )
    return {
        "visited_samples": visited,
        "visited_complete": visited_complete,
        "terminal_grading_samples": graded,
        "grading_complete": grading_complete,
        "controllers_complete": controllers_complete,
        "engine_failed_samples": sum(
            row.get("status") not in terminal and row.get("agent_status") in {"failed", "cancelled"}
            for row in rows
        ),
        "finished": (
            grading_complete
            and controllers_complete
            and not (wave.get("execution") or {}).get("timed_out", False)
            and not wave.get("error_type")
        ),
        "circuit_breaker": wave_blocker(wave),
    }


async def predictions(db, entries):
    targets = {key: value for entry in entries for key, value in entry["targets"].items()}
    metadata = {row["sample_id"]: row for entry in entries for row in entry["rows"]}
    datasets = {
        sample.sample_id: entry["dataset"]
        for entry in entries
        for sample in entry["dataset"].samples
    }
    async with db.snapshot_sessions.begin() as session:
        items = list(await session.scalars(select(GradingItem)))
        assessments = list(await session.scalars(select(Assessment)))
        calls = list(await session.scalars(select(ModelCall)))
        steps = list(await session.scalars(select(Step)))
        runs = list(await session.scalars(select(AgentRun)))
        events = list(await session.scalars(select(AgentEvent)))
        attempts = list(await session.scalars(select(Attempt)))
        reservations = list(await session.scalars(select(AgentModelReservation)))
    by_answer = {str(item.answer_version_id): item for item in items}
    by_run = {str(run.id): run for run in runs}
    completed = {
        str(event.run_id): event.created_at for event in events if event.kind == "completed"
    }
    failures_by_run = defaultdict(list)
    for event in sorted(events, key=lambda value: value.sequence):
        if event.kind in {"model_failed", "model_unknown"}:
            failures_by_run[str(event.run_id)].append(model_failure_receipt(event))
    output = []
    for answer_id, target in targets.items():
        row = dict(metadata[target["sample"].sample_id])
        dataset = datasets[row["sample_id"]]
        item = by_answer.get(answer_id)
        run = by_run[target["run_id"]]
        selected = {
            assessment.criterion_id: assessment.level_id
            for assessment in assessments
            if item and assessment.item_id == item.id
        }
        score = item.suggested_score if item and item.status == "succeeded" else None
        finished = [
            attempt.finished_at
            for attempt in attempts
            if item
            and attempt.item_id == item.id
            and attempt.status == "succeeded"
            and attempt.finished_at
        ]
        receipts = [call for call in calls if item and call.item_id == item.id]
        row.update(
            status=("succeeded" if score is not None else "abstained")
            if item and item.status == "succeeded"
            else item.status
            if item
            else "not_created",
            predicted_score=str(score) if score is not None else None,
            selected_levels=selected,
            band_consistent=band_consistent(dataset.rubric.criteria, selected)
            if row["task_type"] == "numeric" and score is not None
            else None,
            rubric_hash=digest(dataset.rubric.model_dump(mode="json")),
            criterion_count=len(dataset.rubric.criteria),
            answer_hash=target["answer_hash"],
            grading_item_id=str(item.id) if item else None,
            grading_profile=item.evaluation_profile if item else None,
            agent_run_id=str(run.id),
            agent_status=run.status,
            agent_error_code=run.last_error,
            agent_model_failures=failures_by_run[str(run.id)],
            grading_seconds=(finished[-1] - item.created_at).total_seconds() if finished else None,
            agent_seconds=(completed[str(run.id)] - run.created_at).total_seconds()
            if str(run.id) in completed
            else None,
            calls=[
                {
                    "kind": call.kind,
                    "status": call.status,
                    "error_code": call.error_code,
                    "http_status": call.http_status,
                    "input_hash": call.input_hash,
                    "request_id": call.provider_request_id,
                    "returned_model": call.returned_model,
                    "usage": call.usage,
                    "duration_ms": call.duration_ms,
                }
                for call in receipts
            ],
            evaluations={
                step.criterion_id: step.output
                for step in steps
                if item and step.item_id == item.id and step.kind == "evaluate"
            },
        )
        output.append(row)
    controller = {
        "requests": len(reservations),
        "settled": sum(value.settled_at is not None for value in reservations),
        "input_tokens": sum(value.actual_input_tokens or 0 for value in reservations),
        "output_tokens": sum(value.actual_output_tokens or 0 for value in reservations),
        "reported_cost_usd": str(sum(value.settled_cost_usd or 0 for value in reservations)),
        "cost_complete": all(value.settled_cost_usd is not None for value in reservations),
        "failed_http_statuses": dict(
            Counter(
                str(failure["http_status"])
                for failures in failures_by_run.values()
                for failure in failures
                if failure.get("http_status", 0) >= 400
            )
        ),
        "error_codes": dict(
            Counter(
                failure["error_code"]
                for failures in failures_by_run.values()
                for failure in failures
                if failure.get("error_code")
            )
        ),
        "terminal_error_codes": dict(
            Counter(
                run.last_error
                for run in runs
                if run.status in {"failed", "cancelled"} and run.last_error
            )
        ),
        "agent_statuses": dict(Counter(run.status for run in runs)),
        "model_failure_events": sum(len(failures) for failures in failures_by_run.values()),
    }
    return output, controller


async def run_wave(args, groups, budget, index):
    url = checked_url(os.environ["TA_TEST_DATABASE_URL"]).render_as_string(hide_password=False)
    schema = "public_grading_" + uuid4().hex
    admin, db = create_async_engine(url), Database(url, schema=schema)
    settings = Settings(database_url=url, dev_api_token="public-eval-" + uuid4().hex)
    result = {
        "index": index,
        "started_at": datetime.now(UTC).isoformat(),
        "schema_cleaned": False,
        "expected_samples": sum(len(group["rows"]) for group in groups),
        "dataset_ids": [group["dataset"].dataset_id for group in groups],
        "request_start": budget.used,
        "audits": [],
        "observations": [],
    }
    created = False
    entries = []
    active_path = args.output / "active-wave.json"
    save_json(active_path, {"index": index, "schema": schema, "dataset_ids": result["dataset_ids"]})
    try:
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            created = True
        async with db.engine.begin() as connection:

            def migrate(sync):
                config = Config(str(REPO / "alembic.ini"))
                config.attributes["connection"] = sync
                command.upgrade(config, "head")

            await connection.run_sync(migrate)
        await bootstrap(db, settings)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(settings, db)),
            base_url="http://public-benchmark",
            headers={"Authorization": f"Bearer {settings.dev_api_token}"},
        ) as client:
            for group in groups:
                seed_args = SimpleNamespace(
                    runs=1,
                    answers=len(group["rows"]),
                    controller="deepseek",
                    deadline=args.deadline,
                )
                targets, run_ids = await seed(client, group["dataset"], seed_args, None)
                entries.append({**group, "targets": targets, "run_ids": run_ids})
            execution_args = SimpleNamespace(workers=args.workers, deadline=args.deadline)
            result["execution"] = await execute(db, settings, execution_args, budget, None)
            for entry in entries:
                audit_args = SimpleNamespace(mode="live", runs=1, slo_seconds=args.slo_seconds)
                result["audits"].append(
                    await audit(
                        db,
                        entry["dataset"],
                        entry["targets"],
                        audit_args,
                        run_ids=entry["run_ids"],
                    )
                )
            result["observations"], result["controller_usage"] = await predictions(db, entries)
        result["finished"] = (
            len(result["observations"]) == result["expected_samples"]
            and not result["execution"]["timed_out"]
        )
    except (Exception, asyncio.CancelledError) as error:
        result["error_type"] = type(error).__name__
        result["finished"] = False
        if entries:
            try:
                result["observations"], result["controller_usage"] = await asyncio.shield(
                    predictions(db, entries)
                )
            except Exception as export_error:
                result["evidence_export_error"] = type(export_error).__name__
    finally:
        result["request_end"] = budget.used
        result.update(wave_completion(result))
        result["finished_at"] = datetime.now(UTC).isoformat()
        # Save evidence before destroying the isolated schema, even on failure.
        path = args.output / "waves" / f"{index:05d}.json"
        save_json(path, result)
        await db.close()
        if created and "evidence_export_error" not in result:
            async with admin.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            result["schema_cleaned"] = True
        await admin.dispose()
        save_json(path, result)
        if result["schema_cleaned"]:
            active_path.unlink()
    return result


def summarize(waves, planned, budget):
    expected = len(planned)
    observations = [row for wave in waves for row in wave["observations"]]
    observed_ids = {row["sample_id"] for row in observations}
    observations.extend(
        {
            **row,
            "status": "not_attempted",
            "predicted_score": None,
            "band_consistent": None,
            "selected_levels": {},
            "calls": [],
            "agent_status": "not_created",
            "grading_seconds": None,
            "agent_seconds": None,
        }
        for row in planned
        if row["sample_id"] not in observed_ids
    )
    valid = [row for row in observations if row.get("label_valid", True)]
    audits = [audit_result for wave in waves for audit_result in wave["audits"]]
    numeric = [row for row in valid if row["task_type"] == "numeric"]
    decoded = [row for row in numeric if row["predicted_score"] is not None]
    consistent_correct = sum(
        row["band_consistent"] is True and float(row["predicted_score"]) == float(row["gold_score"])
        for row in decoded
    )
    duplicates = {}
    for row in valid:
        duplicates.setdefault((row["dataset"], row["question_answer_hash"]), []).append(row)
    conflicting = [
        rows for rows in duplicates.values() if len({row["gold_score"] for row in rows}) > 1
    ]
    deduplicated = [
        rows[0] for rows in duplicates.values() if len({row["gold_score"] for row in rows}) == 1
    ]
    return {
        "schema_version": "public-jev-agent-report-v1",
        "complete": len(observed_ids) == expected and all(wave.get("finished") for wave in waves),
        "expected_samples": expected,
        "observed_samples": len(observed_ids),
        "invalid_source_labels": len(observations) - len(valid),
        "quality": summarize_public_grading(valid),
        "deduplicated_nonconflicting_quality": summarize_public_grading(deduplicated),
        "conflicting_duplicate_groups": len(conflicting),
        "duplicate_extra_occurrences": sum(len(rows) - 1 for rows in duplicates.values()),
        "encoding": {
            "scored_numeric": len(decoded),
            "inconsistent": sum(row["band_consistent"] is False for row in decoded),
            "exact_and_consistent_rate_all": consistent_correct / len(numeric) if numeric else None,
            "direct_quality": summarize_public_grading(
                [row for row in numeric if row["criterion_count"] == 1]
            ),
            "segmented_quality": summarize_public_grading(
                [row for row in numeric if row["criterion_count"] > 1]
            ),
        },
        "engineering": {
            "invariants_passed": bool(audits)
            and all(value["invariants_passed"] for value in audits),
            "violations": dict(sum((Counter(value["violations"]) for value in audits), Counter())),
            "all_schemas_cleaned": all(wave["schema_cleaned"] for wave in waves),
            "worker_errors": dict(
                sum(
                    (Counter(wave.get("execution", {}).get("worker_errors", {})) for wave in waves),
                    Counter(),
                )
            ),
            "circuit_breakers": [wave_blocker(wave) for wave in waves if wave_blocker(wave)],
            "completed_agents": sum(value["completed_agents"] for value in audits),
            "agents": len(audits),
            "end_to_end_scored": sum(
                row["status"] == "succeeded" and row["agent_status"] == "completed"
                for row in observations
            ),
            "status_counts": dict(Counter(row["status"] for row in observations)),
            "end_to_end_scored_rate_all_planned": sum(
                row["status"] == "succeeded" and row["agent_status"] == "completed"
                for row in observations
            )
            / expected
            if expected
            else None,
            "provider_error_counts": dict(
                Counter(
                    call["error_code"]
                    for row in observations
                    for call in row["calls"]
                    if call["error_code"]
                )
            ),
            "grading_seconds": percentiles(
                [
                    row["grading_seconds"]
                    for row in observations
                    if row["grading_seconds"] is not None
                ]
            ),
            "agent_seconds": percentiles(
                [row["agent_seconds"] for row in observations if row["agent_seconds"] is not None]
            ),
            "provider_milliseconds": percentiles(
                [
                    call["duration_ms"]
                    for row in observations
                    for call in row["calls"]
                    if call["kind"] == "evaluate" and call["duration_ms"] is not None
                ]
            ),
        },
        "real_jev_request_reservations": budget.used,
        "max_jev_request_reservations": budget.maximum,
        "controller_usage_by_wave": [wave.get("controller_usage") for wave in waves],
        "limitations": [
            "SAS answers are LLM generated and human scored; SemEval answers are real student text.",
            "Numeric grading uses fixed total-score band encoding, not original step scoring.",
            "Official duplicate occurrences are retained in primary metrics; content-deduplicated metrics exclude conflicting labels.",
            "Single-process concurrent workers and ASGI submission; no HTTP ingress capacity claim.",
            "Unknown or failed model requests retain their reservations. Jev cost unavailable without verified pricing.",
        ],
    }


async def main(args):
    load_dotenv(REPO / ".env")
    suite = build_suite(args.data)
    if args.dataset != "all":
        suite = [group for group in suite if group["rows"][0]["dataset"] == args.dataset]
    requested_ids = None
    if args.sample_ids_file:
        requested_ids = json.loads(args.sample_ids_file.read_text(encoding="utf-8"))
        if (
            not isinstance(requested_ids, list)
            or any(not isinstance(value, str) for value in requested_ids)
            or len(set(requested_ids)) != len(requested_ids)
        ):
            raise ValueError("Sample-ID selection must contain unique strings")
        suite = select_ids(suite, set(requested_ids))
    suite = select_samples(suite, args.max_samples, args.sample_policy)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "waves").mkdir(exist_ok=True)
    manifest = {
        "controller": "deepseek",
        "workers": args.workers,
        "wave_questions": args.wave_questions,
        "deadline": args.deadline,
        "slo_seconds": args.slo_seconds,
        "max_provider_calls": args.max_provider_calls,
        "sample_policy": args.sample_policy,
        "requested_sample_ids_hash": digest(sorted(requested_ids))
        if requested_ids is not None
        else None,
        "suite_hash": digest([group["dataset"].model_dump(mode="json") for group in suite]),
        "metadata_hash": digest([group["rows"] for group in suite]),
        "source_hash": text_digest((args.data / "manifest.json").read_text(encoding="utf-8")),
        "expected_samples": sum(len(group["rows"]) for group in suite),
        "expected_minimum_jev_calls": sum(
            len(group["rows"]) * len(group["dataset"].rubric.criteria) for group in suite
        ),
        "dataset_shards": len(suite),
        "jev_profile": configured_jev_profile(Settings()).manifest,
        "controller_model": Settings().deepseek_model,
        "source_fingerprint": digest(
            {
                path.relative_to(REPO).as_posix(): text_digest(path.read_text(encoding="utf-8"))
                for folder in (REPO / "backend/src", REPO / "backend/tools")
                for path in sorted(folder.rglob("*.py"))
            }
        ),
    }
    manifest_path = args.output / "run-manifest.json"
    if manifest_path.exists() and json.loads(manifest_path.read_text(encoding="utf-8")) != manifest:
        raise ValueError("Resume configuration differs from the pinned run manifest")
    save_json(manifest_path, manifest)
    budget = DurableBudget(args.output / "request-reservations.jsonl", args.max_provider_calls)
    if (args.output / "active-wave.json").exists():
        raise ValueError("An interrupted wave must be inspected before any request replay")
    waves = []
    print(json.dumps({"stage": "start", **manifest}), flush=True)
    for index, offset in enumerate(range(0, len(suite), args.wave_questions)):
        path = args.output / "waves" / f"{index:05d}.json"
        if path.exists():
            wave = json.loads(path.read_text(encoding="utf-8"))
            if not wave.get("finished") or not wave["schema_cleaned"]:
                raise ValueError("Incomplete wave requires inspection before replay")
        else:
            wave = await run_wave(args, suite[offset : offset + args.wave_questions], budget, index)
        waves.append(wave)
        blocker = wave_blocker(wave)
        save_json(
            args.output / "progress.json",
            {
                "waves_finished": len(waves),
                "waves_total": (len(suite) + args.wave_questions - 1) // args.wave_questions,
                "samples_finished": sum(len(value["observations"]) for value in waves),
                "samples_total": manifest["expected_samples"],
                "jev_requests": budget.used,
                "last_wave_error": wave.get("error_type"),
                "circuit_breaker": blocker,
            },
        )
        print(
            json.dumps(
                {
                    "stage": "wave",
                    "index": index,
                    "samples": len(wave["observations"]),
                    "jev_requests": budget.used,
                    "finished": wave.get("finished"),
                    "error_type": wave.get("error_type"),
                    "circuit_breaker": blocker,
                }
            ),
            flush=True,
        )
        if blocker or not wave.get("finished"):
            break
    planned = [
        {**row, "criterion_count": len(group["dataset"].rubric.criteria)}
        for group in suite
        for row in group["rows"]
    ]
    report = summarize(waves, planned, budget)
    save_json(args.output / "report.json", report)
    save_json(
        args.output / "observations.json", [row for wave in waves for row in wave["observations"]]
    )
    print(
        json.dumps(
            {
                "stage": "complete",
                "complete": report["complete"],
                "samples": report["observed_samples"],
                "jev_requests": budget.used,
                "engineering": report["engineering"],
            }
        ),
        flush=True,
    )
    return report["complete"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".local/datasets/public-grading"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dataset", choices=("all", "sas-bench", "semeval"), default="all")
    parser.add_argument("--workers", type=int, choices=range(1, 17), default=8)
    parser.add_argument("--wave-questions", type=int, choices=range(1, 33), default=16)
    parser.add_argument("--deadline", type=int, default=1800)
    parser.add_argument("--slo-seconds", type=int, default=120)
    parser.add_argument("--max-provider-calls", type=int, default=50000)
    parser.add_argument("--max-samples", type=int, default=0)
    parser.add_argument("--sample-policy", choices=("prefix", "balanced-v1"), default="prefix")
    parser.add_argument("--sample-ids-file", type=Path)
    args = parser.parse_args()
    if (
        not 10 <= args.deadline <= 3600
        or not 1 <= args.max_provider_calls <= 100000
        or not 0 <= args.max_samples <= 20000
        or not 0 < args.slo_seconds <= 3600
    ):
        parser.error("Deadline or request budget outside bounded limits")
    with OutputLock(args.output):
        raise SystemExit(0 if asyncio.run(main(args)) else 1)

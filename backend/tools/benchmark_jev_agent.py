"""Bounded burst benchmark: Harness tools -> Jev -> PostgreSQL -> collected result.

Default is a protocol stub, NOT semantic evaluation. Live mode is explicit, uses
the configured Jev credentials, and caps request attempts before dispatch.
Only a random schema in a dedicated *_test database is ever written.
"""

import argparse
import asyncio
import json
import math
import os
import platform
from collections import Counter
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from time import monotonic
from uuid import UUID, uuid4

import httpx
from alembic import command
from alembic.config import Config
from dotenv import load_dotenv
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from teaching_assistant.api import create_app
from teaching_assistant.cli import bootstrap
from teaching_assistant.comparison import load_dataset
from teaching_assistant.db import (
    AgentArtifact,
    AgentEvent,
    AgentRun,
    AnswerVersion,
    Assessment,
    Attempt,
    Database,
    GradingItem,
    ModelCall,
    Step,
)
from teaching_assistant.domain import ModelError, calculate_score, digest, text_digest
from teaching_assistant.evaluators import ManualEvidenceExtractor
from teaching_assistant.harness.worker import HarnessWorker
from teaching_assistant.jev import JevEvaluator
from teaching_assistant.settings import Settings
from teaching_assistant.worker import Worker

REPO = Path(__file__).resolve().parents[2]


def checked_url(value):
    url = make_url(value)
    if url.drivername != "postgresql+asyncpg" or not (url.database or "").endswith("_test"):
        raise ValueError("A separate PostgreSQL *_test database is required")
    return url


def percentiles(values):
    ordered = sorted(values)
    return {
        name: round(ordered[max(0, math.ceil(p * len(ordered)) - 1)], 4) if ordered else None
        for name, p in (("p50", 0.5), ("p95", 0.95), ("p99", 0.99), ("max", 1))
    }


class ProviderBudget:
    def __init__(self, maximum):
        self.maximum, self.used = maximum, 0

    def reserve(self):
        # No await between comparison and increment; all slots share this event loop.
        if self.used >= self.maximum:
            raise ModelError("benchmark_call_budget_exhausted", retryable=False)
        self.used += 1  # Unknown/time-out requests are never refunded.


class BoundedJev:
    def __init__(self, adapter, budget):
        self.adapter, self.budget = adapter, budget

    def __getattr__(self, name):
        return getattr(self.adapter, name)

    async def assess(self, request):
        self.budget.reserve()
        return await self.adapter.assess(request)


class BenchmarkWorker(Worker):
    def __init__(self, db, settings, budget, provider_client):
        super().__init__(db, settings)
        self.budget, self.provider_client = budget, provider_client

    def adapters(self, profile_id):
        if profile_id not in self._adapters:
            profile = self.profiles.resolve(profile_id)
            self._adapters[profile_id] = (
                BoundedJev(
                    JevEvaluator(self.settings, profile, client=self.provider_client), self.budget
                ),
                ManualEvidenceExtractor(),
            )
        return self._adapters[profile_id]


class ProtocolStub:
    def __init__(self, dataset, latency, faults):
        self.dataset, self.latency, self.faults = dataset, latency, faults
        self.answers, self.seen, self.injected = {}, Counter(), Counter()

    def register(self, text_value, sample, index):
        fault = index % 8 if self.faults else 0
        levels = {
            criterion.id: (sample.gold_levels or {}).get(
                criterion.id, sorted(criterion.levels, key=lambda level: level.ordinal)[0].id
            )
            for criterion in self.dataset.rubric.criteria
        }
        self.answers[text_value] = (levels, fault)
        return {key: None for key in levels} if fault == 4 else levels, fault == 5

    async def respond(self, request):
        await asyncio.sleep(self.latency)
        payload = json.loads(request.content)
        answer = payload["state"]["student_answer"]
        self.seen[answer] += 1
        levels, fault = self.answers[answer]
        if self.seen[answer] == 1 and fault in {1, 2, 3, 5, 6}:
            self.injected[fault] += 1
            if fault == 6:
                raise httpx.ReadTimeout("synthetic timeout", request=request)
            if fault == 3:
                return httpx.Response(200, json={"model": payload["model"], "answers": {}})
            return httpx.Response({1: 429, 2: 503, 5: 401}[fault], headers={"Retry-After": "0"})
        point = next(
            c
            for c in self.dataset.rubric.criteria
            if c.description == payload["questions"]["criterion"]["instructions"]["criterion"]
        )
        ordered = sorted(point.levels, key=lambda level: level.ordinal)
        selected = next(i for i, level in enumerate(ordered) if level.id == levels[point.id])
        probabilities = {
            str(i): 1 / len(ordered) if fault == 4 else float(i == selected)
            for i in range(len(ordered))
        }
        return httpx.Response(
            200,
            json={
                "model": payload["model"],
                "answers": {
                    "criterion": {
                        "type": "score",
                        "score": sum(int(i) * p for i, p in probabilities.items()),
                        "confidence": 0 if fault == 4 else 1,
                        "legend": {str(i): level.description for i, level in enumerate(ordered)},
                        "probabilities": probabilities,
                    }
                },
                "usage": {"input_tokens": 10, "output_tokens": 3},
            },
            headers={"x-typesafe-request-id": "fixture-" + uuid4().hex},
        )


async def post(client, path, body, key=None):
    response = await client.post(path, json=body, headers={"Idempotency-Key": key or uuid4().hex})
    if response.is_error:
        raise RuntimeError(f"Benchmark API rejected request: {response.status_code}")
    return response.json()


async def seed(client, dataset, args, stub):
    targets, runs = {}, []
    for run_index in range(args.runs):
        workspace = await post(
            client,
            "/v1/workspaces",
            {
                "course_name": "Jev agent benchmark",
                "classroom_name": f"Benchmark {run_index}",
                "students": [
                    {"external_id": f"fixture-{i}", "name": f"Benchmark {i}"}
                    for i in range(args.answers)
                ],
            },
        )
        assignment = await post(
            client,
            f"/v1/courses/{workspace['course_id']}/assignments",
            {
                "classroom_id": workspace["classroom_id"],
                "title": "Jev benchmark",
            },
        )
        question = await post(
            client,
            f"/v1/assignments/{assignment['id']}/question-versions",
            {
                "question_key": "q1",
                "prompt": dataset.question,
                "reference": dataset.reference,
                "maximum_points": str(sum(c.maximum_points for c in dataset.rubric.criteria)),
            },
        )
        draft = await post(
            client,
            f"/v1/question-versions/{question['id']}/rubric-drafts",
            {
                "rubric": dataset.rubric.model_dump(mode="json"),
            },
        )
        rubric = await post(
            client, f"/v1/rubric-drafts/{draft['id']}/confirm", {"expected_revision": 1}
        )
        rows, expected = [], []
        for i, student in enumerate(workspace["students"]):
            index = run_index * args.answers + i
            sample = dataset.samples[index % len(dataset.samples)]
            answer = sample.answer_text
            if stub:
                answer += f"\n[protocol-fixture:{index}]"
            levels, failed = stub.register(answer, sample, index) if stub else (None, False)
            expected.append(
                {
                    "sample": sample,
                    "levels": levels,
                    "failed": failed,
                    "answer_hash": text_digest(answer),
                    "rubric_id": rubric["id"],
                    "question_id": question["id"],
                }
            )
            rows.append(
                {
                    "student_id": student["id"],
                    "question_version_id": question["id"],
                    "external_submission_id": f"fixture-{i}",
                    "source_revision": "v1",
                    "answer_text": answer,
                }
            )
        imported = await post(client, f"/v1/assignments/{assignment['id']}/imports", {"rows": rows})
        targets.update(zip(imported["answer_version_ids"], expected, strict=True))
        body = {
            "rubric_version_id": rubric["id"],
            "answer_version_ids": imported["answer_version_ids"],
            "agent_profile": f"{args.controller}-grading-agent-v1",
            "evaluation_profile": "jev-v1",
            "max_seconds": args.deadline,
            "max_model_calls": 6,
            "max_tool_calls": 8,
            "max_total_tokens": 120000,
        }
        key = uuid4().hex
        run = await post(client, "/v1/grading-agent-runs", body, key)
        repeated = await post(client, "/v1/grading-agent-runs", body, key)
        if repeated != run:
            raise AssertionError("Idempotent run submission changed its receipt")
        runs.append(UUID(run["id"]))
        for target in expected:
            target["run_id"] = run["id"]
    return targets, runs


async def execute(db, settings, args, budget, provider_client):
    stopped = asyncio.Event()
    errors = Counter()
    available = None
    harnesses = []
    for _ in range(args.workers):
        worker = HarnessWorker(db, settings, available_profiles=available)
        available = worker.available
        harnesses.append(worker)
    workers = harnesses + [
        BenchmarkWorker(db, settings, budget, provider_client) for _ in range(args.workers)
    ]

    async def work(worker):
        while not stopped.is_set():
            try:
                processed = await worker.run_once()
            except Exception as exc:
                errors[type(exc).__name__] += 1
                processed = False
            if not processed:
                await asyncio.sleep(0.05)

    tasks = [asyncio.create_task(work(worker)) for worker in workers]
    started = monotonic()
    timed_out = False
    try:
        while True:
            async with db.sessions() as session:
                active_runs = await session.scalar(
                    select(func.count())
                    .select_from(AgentRun)
                    .where(AgentRun.status.not_in(("completed", "failed", "cancelled")))
                )
                active_items = await session.scalar(
                    select(func.count())
                    .select_from(GradingItem)
                    .where(GradingItem.status.not_in(("succeeded", "failed")))
                )
            if not active_runs and not active_items:
                break
            if monotonic() - started > args.deadline + 10:
                timed_out = True
                break
            await asyncio.sleep(0.1)
    finally:
        stopped.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return {
        "drain_seconds": round(monotonic() - started, 4),
        "timed_out": timed_out,
        "worker_errors": dict(errors),
    }


def label_metrics(dataset, observed, *, live):
    if not live or dataset.label_status == "unlabeled":
        return None
    labeled = [(target, levels) for target, levels in observed if target["sample"].gold_levels]
    total = sum(len(target["sample"].gold_levels) for target, _ in labeled)
    answered = correct = 0
    differences = []
    for target, levels in labeled:
        gold = target["sample"].gold_levels
        for key, expected in gold.items():
            answered += levels.get(key) is not None
            correct += levels.get(key) == expected
        score = calculate_score(
            dataset.rubric, {c.id: levels.get(c.id) for c in dataset.rubric.criteria}
        )
        if score is not None:
            differences.append(score - calculate_score(dataset.rubric, gold))
    return {
        "label_status": dataset.label_status,
        "split": dataset.split,
        "independent_sample_count": len({target["sample"].sample_id for target, _ in labeled}),
        "answer_observations": len(labeled),
        "labeled_points": total,
        "answered_points": answered,
        "correct_points": correct,
        "point_coverage": answered / total if total else None,
        "point_accuracy_on_answered": correct / answered if answered else None,
        "end_to_end_correct_point_rate": correct / total if total else None,
        "complete_answer_count": len(differences),
        "total_score_mae_on_complete": float(sum(map(abs, differences)) / len(differences))
        if differences
        else None,
        "note": "Repeated answers are not independent quality samples. Missing/failed predictions remain in the end-to-end denominator.",
    }


async def audit(db, dataset, targets, args, *, run_ids=None):
    async with db.snapshot_sessions.begin() as session:
        rows = {}
        for model in (
            AgentRun,
            AgentEvent,
            AgentArtifact,
            GradingItem,
            AnswerVersion,
            Assessment,
            Step,
            ModelCall,
            Attempt,
        ):
            query = select(model)
            if run_ids is not None:
                answer_ids = [UUID(key) for key in targets]
                item_ids = select(GradingItem.id).where(
                    GradingItem.answer_version_id.in_(answer_ids)
                )
                if model is AgentRun:
                    query = query.where(AgentRun.id.in_(run_ids))
                elif model in (AgentEvent, AgentArtifact):
                    query = query.where(model.run_id.in_(run_ids))
                elif model is GradingItem:
                    query = query.where(GradingItem.answer_version_id.in_(answer_ids))
                elif model is AnswerVersion:
                    query = query.where(AnswerVersion.id.in_(answer_ids))
                else:
                    query = query.where(model.item_id.in_(item_ids))
            rows[model] = list(await session.scalars(query))
    violations = Counter()

    def check(condition, code):
        if not condition:
            violations[code] += 1

    items, calls, steps = rows[GradingItem], rows[ModelCall], rows[Step]
    points = {c.id: {level.id: level.points for level in c.levels} for c in dataset.rubric.criteria}
    calls_by_id, steps_by_id = {c.id: c for c in calls}, {s.id: s for s in steps}
    assessments_by_item, steps_by_item, calls_by_item, attempts_by_item = {}, {}, {}, {}
    for model, groups in (
        (Assessment, assessments_by_item),
        (Step, steps_by_item),
        (ModelCall, calls_by_item),
        (Attempt, attempts_by_item),
    ):
        for row in rows[model]:
            groups.setdefault(row.item_id, []).append(row)
    check(len(items) == len(targets), "missing_or_duplicate_item")
    check(len({item.answer_version_id for item in items}) == len(items), "duplicate_answer_target")
    observations, latency, claim_latency, on_time = [], [], [], 0
    for item in items:
        target = targets[str(item.answer_version_id)]
        assessed = assessments_by_item.get(item.id, [])
        selected = (
            {a.criterion_id: a.level_id for a in assessed} if item.status == "succeeded" else {}
        )
        observations.append((target, selected))
        check(str(item.rubric_version_id) == target["rubric_id"], "wrong_rubric")
        check(item.review_revision == 0, "unexpected_teacher_confirmation")
        check(item.lease_owner is item.lease_until is None, "residual_lease")
        check(
            item.calls_used == len(calls_by_item.get(item.id, [])) <= item.max_calls,
            "call_accounting",
        )
        check(item.attempts <= item.max_attempts, "attempt_budget_exceeded")
        if args.mode == "stub":
            check(
                item.status == ("failed" if target["failed"] else "succeeded"),
                "unexpected_fixture_status",
            )
        attempts = attempts_by_item.get(item.id, [])
        if attempts:
            claim_latency.append(
                (min(a.created_at for a in attempts) - item.created_at).total_seconds()
            )
        if item.status != "succeeded":
            check(item.suggested_score is None, "failure_has_score")
            continue
        check(
            set(selected) == set(points) and len(assessed) == len(points), "incomplete_assessments"
        )
        check(len(steps_by_item.get(item.id, [])) == 2 * len(points), "missing_or_duplicate_step")
        check(
            item.suggested_score == calculate_score(dataset.rubric, selected), "incorrect_score_sum"
        )
        if args.mode == "stub":
            check(selected == target["levels"], "fixture_roundtrip_mismatch")
        for assessment in assessed:
            check(
                assessment.suggested_points
                == points[assessment.criterion_id].get(assessment.level_id),
                "wrong_level_points",
            )
            check(
                assessment.evidence.get("answer_hash") == target["answer_hash"],
                "wrong_evidence_hash",
            )
            for kind, step_id in (
                ("evaluate", assessment.evaluation_step_id),
                ("extract_evidence", assessment.evidence_step_id),
            ):
                step = steps_by_id[step_id]
                call = calls_by_id[step.call_id]
                check(
                    step.item_id == item.id
                    and step.criterion_id == assessment.criterion_id
                    and step.kind == kind
                    and call.status == "succeeded"
                    and call.input_hash == step.input_hash,
                    "checkpoint_receipt_mismatch",
                )
        finished = [a.finished_at for a in attempts if a.status == "succeeded" and a.finished_at]
        check(len(finished) == 1, "terminal_attempt_mismatch")
        if finished:
            seconds = (finished[0] - item.created_at).total_seconds()
            latency.append(seconds)
            on_time += seconds <= args.slo_seconds
    # Include missing/uncreated targets in quality's denominator, too.
    present = {str(item.answer_version_id) for item in items}
    observations.extend((target, {}) for key, target in targets.items() if key not in present)
    for answer in rows[AnswerVersion]:
        check(
            answer.content_hash
            == text_digest(answer.answer_text)
            == targets[str(answer.id)]["answer_hash"],
            "answer_changed",
        )
    check(all(c.status in {"succeeded", "failed", "unknown"} for c in calls), "unclosed_call")
    artifact_by_run = {a.run_id: a for a in rows[AgentArtifact]}
    check(len(artifact_by_run) == len(rows[AgentArtifact]), "duplicate_artifact")
    root_latency = []
    for run in rows[AgentRun]:
        events = sorted(
            (e for e in rows[AgentEvent] if e.run_id == run.id), key=lambda e: e.sequence
        )
        check([e.sequence for e in events] == list(range(1, run.event_seq + 1)), "event_gap")
        success_tools = [
            e.body["name"]
            for e in events
            if e.kind == "post_tool" and e.body.get("output", {}).get("ok")
        ]
        check(run.status == "completed", "agent_not_completed")
        check(
            success_tools == ["read_grading_task", "grade_homework", "collect_grading_result"],
            "tool_chain_mismatch",
        )
        artifact = artifact_by_run.get(run.id)
        check(
            bool(
                artifact
                and artifact.content_hash == digest(artifact.body)
                and artifact.confirmed_at is None
            ),
            "invalid_artifact",
        )
        requested = [c["id"] for message in run.messages for c in message.get("tool_calls", [])]
        received = [m["tool_call_id"] for m in run.messages if m.get("role") == "tool"]
        check(
            Counter(requested) == Counter(received)
            and all(v == 1 for v in Counter(received).values()),
            "tool_receipt_mismatch",
        )
        if run.status == "completed":
            root_latency.append((events[-1].created_at - run.created_at).total_seconds())
    statuses = Counter(item.status for item in items)
    scored = sum(item.status == "succeeded" and item.suggested_score is not None for item in items)
    completed_roots = {str(run.id): run for run in rows[AgentRun] if run.status == "completed"}
    finishes = {str(e.run_id): e.created_at for e in rows[AgentEvent] if e.kind == "completed"}
    end_to_end = [
        item
        for item in items
        if item.status == "succeeded"
        and targets[str(item.answer_version_id)]["run_id"] in completed_roots
    ]
    end_to_end_on_time = sum(
        (
            finishes[targets[str(item.answer_version_id)]["run_id"]]
            - completed_roots[targets[str(item.answer_version_id)]["run_id"]].created_at
        ).total_seconds()
        <= args.slo_seconds
        for item in end_to_end
    )
    provider_calls = [call for call in calls if call.kind == "evaluate"]
    quality = label_metrics(dataset, observations, live=args.mode == "live")
    return {
        "submitted_answers": len(targets),
        "grading_statuses": dict(statuses),
        "completed_agents": sum(run.status == "completed" for run in rows[AgentRun]),
        "agent_completion_rate": len(completed_roots) / args.runs,
        "end_to_end_success_rate": len(end_to_end) / len(targets),
        "end_to_end_on_time_rate": end_to_end_on_time / len(targets),
        "workflow_completion_rate": statuses["succeeded"] / len(targets),
        "scored_answer_rate": scored / len(targets),
        "scored_answers": scored,
        "abstained_answers": statuses["succeeded"] - scored,
        "on_time_completion_rate": on_time / len(targets),
        "slo_seconds": args.slo_seconds,
        "grading_end_to_end_seconds": percentiles(latency),
        "queue_seconds": percentiles(claim_latency),
        "agent_end_to_end_seconds": percentiles(root_latency),
        "latency_population": "Successful items only; failures and nonterminal items remain in rate denominators.",
        "assessments": len(rows[Assessment]),
        "checkpoints": len(steps),
        "adapter_calls": len(calls),
        "call_statuses": dict(Counter(c.status for c in calls)),
        "call_errors": dict(Counter(c.error_code for c in calls if c.error_code)),
        "retried_answers": sum(item.attempts > 1 for item in items),
        "controller_model_calls": sum(run.models_used for run in rows[AgentRun]),
        "grading_profile_ids": sorted({item.evaluation_profile for item in items}),
        "returned_jev_models": dict(
            Counter(call.returned_model for call in provider_calls if call.returned_model)
        ),
        "provider_call_milliseconds": percentiles(
            [call.duration_ms for call in provider_calls if call.duration_ms is not None]
        ),
        "reported_input_tokens": sum(
            (call.usage or {}).get("input_tokens", 0) for call in provider_calls
        ),
        "reported_output_tokens": sum(
            (call.usage or {}).get("output_tokens", 0) for call in provider_calls
        ),
        "provider_calls_without_usage": sum(call.usage is None for call in provider_calls),
        "estimated_cost_usd": None,
        "invariants_passed": not violations,
        "violations": dict(violations),
        "semantic_quality": quality if dataset.label_status == "teacher_confirmed" else None,
        "synthetic_label_agreement": quality if dataset.label_status == "synthetic" else None,
    }


async def benchmark(args):
    load_dotenv(REPO / ".env")
    url = checked_url(os.environ["TA_TEST_DATABASE_URL"]).render_as_string(hide_password=False)
    dataset = load_dataset(args.dataset)
    args.output.mkdir(parents=True, exist_ok=False)
    schema = "jev_benchmark_" + uuid4().hex
    admin, db = create_async_engine(url), Database(url, schema=schema)
    config = {"database_url": url, "dev_api_token": "jev-benchmark-" + uuid4().hex}
    stub = None
    if args.mode == "stub":
        stub = ProtocolStub(dataset, args.latency_ms / 1000, args.faults)
        config.update(
            jev_model="jev-1.14.0",
            jev_api_key="protocol-fixture-only",
            jev_endpoint="http://127.0.0.1/v1/systemone",
            jev_proxy=None,
            deepseek_model=None,
            deepseek_api_key=None,
            deepseek_proxy=None,
            call_timeout_seconds=2,
            lease_seconds=5,
            retry_delay_seconds=0,
        )
    settings = Settings(**config)
    budget = ProviderBudget(args.max_provider_calls)
    report = {
        "schema_version": "jev-agent-benchmark-v1",
        "started_at": datetime.now(UTC).isoformat(),
        "mode": args.mode,
        "controller": args.controller,
        "configuration": vars(args).copy(),
        "dataset_hash": digest(dataset.model_dump(mode="json")),
        "label_status": dataset.label_status,
        "passed": False,
        "schema_cleaned": False,
        "scope": "Single-process concurrent workers, real PostgreSQL, burst submission via ASGI. No HTTP ingress/network capacity claim.",
        "environment": {
            "python": platform.python_version(),
            "os": platform.system(),
            "cpu_logical": os.cpu_count(),
        },
        "dependency_versions": {
            name: version(name) for name in ("asyncpg", "SQLAlchemy", "httpx", "pydantic")
        },
        "source_fingerprint": digest(
            {
                str(path.relative_to(REPO)): text_digest(path.read_text(encoding="utf-8"))
                for folder in (REPO / "backend/src", REPO / "backend/migrations")
                for path in sorted(folder.rglob("*.py"))
            }
        ),
    }
    report["configuration"] = {
        k: str(v) if isinstance(v, Path) else v for k, v in report["configuration"].items()
    }
    created = False
    provider_client = (
        httpx.AsyncClient(transport=httpx.MockTransport(stub.respond)) if stub else None
    )
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
            base_url="http://benchmark",
            headers={"Authorization": f"Bearer {settings.dev_api_token}"},
        ) as client:
            targets, runs = await seed(client, dataset, args, stub)
            print(
                json.dumps({"stage": "seeded", "agent_runs": len(runs), "answers": len(targets)}),
                flush=True,
            )
            report["execution"] = await execute(db, settings, args, budget, provider_client)
            report["audit"] = await audit(db, dataset, targets, args)
            report["engineering_passed"] = (
                report["audit"]["invariants_passed"]
                and not report["execution"]["timed_out"]
                and not report["execution"]["worker_errors"]
            )
            report["all_answers_succeeded"] = report["audit"]["workflow_completion_rate"] == 1
            report["operational_slo_passed"] = (
                report["audit"]["end_to_end_success_rate"] >= args.min_completion_rate
                and report["audit"]["end_to_end_on_time_rate"] >= args.min_completion_rate
                and report["audit"]["scored_answer_rate"] >= args.min_scored_rate
            )
            report["passed"] = report["engineering_passed"] and (
                args.faults or report["operational_slo_passed"]
            )
            report["acceptance_basis"] = (
                "expected_fault_outcomes" if args.faults else "engineering_and_operational_slo"
            )
    except Exception as exc:
        # SQL/HTTP exception messages can contain sensitive connection or payload values.
        report["error_type"] = type(exc).__name__
    finally:
        report["jev_request_attempts"] = budget.used
        report["real_jev_request_attempts"] = budget.used if args.mode == "live" else 0
        report["injected_faults"] = dict(stub.injected) if stub else {}
        if provider_client:
            await provider_client.aclose()
        await db.close()
        if created:
            async with admin.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            report["schema_cleaned"] = True
        await admin.dispose()
        report["finished_at"] = datetime.now(UTC).isoformat()
        (args.output / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            json.dumps({k: v for k, v in report.items() if k not in {"configuration", "scope"}}),
            flush=True,
        )
    return report["passed"]


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("stub", "live"), default="stub")
    parser.add_argument("--controller", choices=("fake", "deepseek"), default="fake")
    parser.add_argument("--dataset", type=Path, default=REPO / "evals/samples/synthetic-v1.json")
    parser.add_argument("--runs", type=int, default=4)
    parser.add_argument("--answers", type=int, default=20)
    parser.add_argument("--workers", type=int, choices=range(1, 17), default=4)
    parser.add_argument("--deadline", type=int, default=600)
    parser.add_argument("--slo-seconds", type=float, default=60)
    parser.add_argument("--min-completion-rate", type=float, default=0.99)
    parser.add_argument("--min-scored-rate", type=float, default=0.95)
    parser.add_argument("--max-provider-calls", type=int, default=1000)
    parser.add_argument("--latency-ms", type=float, default=150)
    parser.add_argument("--faults", action="store_true")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if not (
        1 <= args.runs <= 50
        and 1 <= args.answers <= 200
        and args.runs * args.answers <= 2000
        and 10 <= args.deadline <= 3600
        and 0 < args.slo_seconds <= 3600
        and 0 <= args.latency_ms <= 1000
        and 1 <= args.max_provider_calls <= 20000
        and 0 <= args.min_completion_rate <= 1
        and 0 <= args.min_scored_rate <= 1
    ):
        parser.error("Workload, deadline or request budget exceeds bounded limits")
    if args.mode == "stub" and args.controller != "fake":
        parser.error("Protocol-stub mode never calls a paid controller")
    if args.mode == "live" and (
        args.faults or args.runs * args.answers > 200 or args.max_provider_calls > 1000
    ):
        parser.error(
            "Live runs allow at most 200 answers / 1000 Jev requests and no injected faults"
        )
    return args


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(benchmark(arguments())) else 1)

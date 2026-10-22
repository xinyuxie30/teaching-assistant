"""Opt-in paid Harness acceptance against an isolated PostgreSQL test schema.

Run from the repository: .venv/Scripts/python.exe backend/tools/verify_harness_real.py
Reading --help never loads credentials, connects to a database, or calls a model.
All teacher/student/course inputs are synthetic; saved artifacts are NOT confirmed.
The summary case explicitly seeds synthetic historical narration, then invokes the
real model. Fixed peak prices are accounting ceilings, never an actual invoice.
"""

import argparse
import asyncio
import json
import os
import re
import secrets
import sys
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal
from pathlib import Path
from time import monotonic
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
from alembic import command
from alembic.config import Config
from dotenv import load_dotenv
from sqlalchemy import or_, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend" / "src"))

from teaching_assistant.api import create_app  # noqa: E402
from teaching_assistant.cli import bootstrap  # noqa: E402
from teaching_assistant.db import (  # noqa: E402
    AgentArtifact,
    AgentContextCheckpoint,
    AgentEvent,
    AgentModelReservation,
    AgentRun,
    AgentToolResult,
    Database,
)
from teaching_assistant.domain import ModelError, digest  # noqa: E402
from teaching_assistant.harness import budgets, catalog, pool, tool_results  # noqa: E402
from teaching_assistant.harness.worker import HarnessWorker  # noqa: E402
from teaching_assistant.settings import Settings  # noqa: E402

MODEL = "deepseek-flash"
INPUT_PRICE, OUTPUT_PRICE, CACHE_PRICE = Decimal("0.3"), Decimal("1.2"), Decimal("0.006")
PRICE_VERSION = "deepseek-flash-2026-10-22-peak-ceiling"
ANSWER = "这是合成验收教师回答：先独立思考两分钟，再两人交流；保留各自不同的理由。"
BRIEF = {
    "topic": "合成课程：记录与引用的可追溯性",
    "objectives": ["说明保存来源与固定版本的意义"],
    "duration_minutes": 10,
}


class VerificationError(Exception):
    """Only constant, non-secret failure codes are printed or persisted."""


def require(condition, code):
    if not condition:
        raise VerificationError(code)


def safe_error(exc):
    code = exc.args[0] if isinstance(exc, VerificationError) and exc.args else None
    if isinstance(exc, ModelError):
        code = exc.code
    if not isinstance(code, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,120}", code):
        code = type(exc).__name__
    return code


def complete_usage(value):
    return isinstance(value, dict) and all(
        type(value.get(key)) is int and value[key] >= 0 for key in ("input_tokens", "output_tokens")
    )


class CallGuard:
    """One process-wide gate, shared by roots, children, and real summaries."""

    def __init__(self, max_calls, max_cost_usd):
        self.max_calls, self.max_cost_usd = max_calls, max_cost_usd
        self.calls, self.denials = [], []
        self.accounted = Decimal(0)
        self.lock = asyncio.Lock()

    async def reserve(self, run_id, body, policy):
        require(policy.get("provider") == "deepseek", "unexpected_non_real_provider")
        require(policy.get("model") == MODEL, "unexpected_model")
        require(policy.get("expected_returned_model") == MODEL, "unexpected_returned_model_policy")
        endpoint = urlsplit(policy.get("endpoint", ""))
        require(
            endpoint.scheme == "https"
            and endpoint.hostname == "api.deepseek.com"
            and endpoint.port in {None, 443}
            and endpoint.path in {"/chat/completions", "/v1/chat/completions"}
            and not (endpoint.username or endpoint.password or endpoint.query or endpoint.fragment),
            "unexpected_model_endpoint",
        )
        bounds = budgets.request_bounds(body, policy)
        require(bounds is not None and bounds["cost_usd"] is not None, "missing_price_bounds")
        # Independently apply pinned uncached peak prices to the conservative
        # token bound, even if a future manifest uses a lower cache estimate.
        cost = (
            (bounds["input_tokens"] * INPUT_PRICE + bounds["output_tokens"] * OUTPUT_PRICE)
            / Decimal(1000000)
        ).quantize(Decimal("0.000000000001"), rounding=ROUND_CEILING)
        async with self.lock:
            if len(self.calls) >= self.max_calls or self.accounted + cost > self.max_cost_usd:
                code = (
                    "real_verification_call_cap"
                    if len(self.calls) >= self.max_calls
                    else "real_verification_cost_cap"
                )
                self.denials.append({"run_id": str(run_id), "code": code})
                raise ModelError(code, retryable=False)
            item = {
                "ordinal": len(self.calls) + 1,
                "run_id": str(run_id),
                "request_hash": digest(body),
                "requested_model": MODEL,
                "started_at": datetime.now(UTC).isoformat(),
                "reserved_input_tokens": bounds["input_tokens"],
                "reserved_output_tokens": bounds["output_tokens"],
                "reserved_cost_usd": str(cost),
                "accounted_cost_usd": str(cost),
                "status": "reserved",
            }
            self.accounted += cost
            self.calls.append(item)
            return item

    async def success(self, item, reply):
        metadata = reply.metadata.model_dump(mode="json")
        async with self.lock:
            item.update(status="succeeded", metadata=metadata)
            if complete_usage(metadata.get("usage")):
                usage = metadata["usage"]
                cost = (
                    (usage["input_tokens"] * INPUT_PRICE + usage["output_tokens"] * OUTPUT_PRICE)
                    / Decimal(1000000)
                ).quantize(Decimal("0.000000000001"), rounding=ROUND_CEILING)
                self.accounted += cost - Decimal(item["accounted_cost_usd"])
                item["accounted_cost_usd"] = str(cost)
                item["usage_complete"] = True
                # An estimator overrun cannot be undone; prevent all subsequent
                # dispatches and fail acceptance instead of hiding the overrun.
                if self.accounted > self.max_cost_usd:
                    raise ModelError("real_verification_cost_overrun", retryable=False)
            else:
                item["usage_complete"] = False

    async def failure(self, item, exc):
        async with self.lock:
            # No exception releases any reservation, including cancellation and
            # timeout: the provider may already have processed the request.
            item.update(status="failed_unknown", error=safe_error(exc), usage_complete=False)
            if isinstance(exc, ModelError):
                item["failure_metadata"] = {
                    "provider_request_id": exc.provider_request_id,
                    "http_status": exc.http_status,
                    "returned_model": exc.returned_model,
                    "usage": exc.usage,
                }

    def snapshot(self):
        return {
            "max_calls": self.max_calls,
            "max_cost_usd": str(self.max_cost_usd),
            "calls_started": len(self.calls),
            "accounted_peak_cost_usd": str(self.accounted),
            "unknown_calls_keep_reservations": True,
            "prices_are_accounting_ceilings_not_invoice": True,
            "calls": self.calls,
            "denials": self.denials,
        }


def guarded_worker_class(guard):
    class GuardedWorker(HarnessWorker):
        async def invoke(self, lease, body, policy, timeout_seconds):
            reservation = await guard.reserve(lease.run_id, body, policy)
            try:
                reply = await super().invoke(lease, body, policy, timeout_seconds)
                await guard.success(reservation, reply)
                return reply
            except BaseException as exc:
                await guard.failure(reservation, exc)
                raise

    return GuardedWorker


async def api_post(client, path, payload):
    response = await client.post(path, json=payload, headers={"Idempotency-Key": uuid4().hex})
    require(response.status_code < 300, f"api_post_{response.status_code}")
    return response.json()


async def api_get(client, path):
    response = await client.get(path)
    require(response.status_code == 200, f"api_get_{response.status_code}")
    return response.json()


async def create_course(client, name):
    result = await api_post(
        client,
        "/v1/workspaces",
        {
            "course_name": f"Synthetic Harness acceptance: {name}",
            "classroom_name": "Synthetic classroom; no real learner data",
            "students": [{"external_id": "synthetic-1", "name": "Synthetic fixture student"}],
        },
    )
    return result["course_id"]


async def create_memories(client, course_id, count, *, large=False):
    items = []
    for index in range(count):
        paragraph = (
            f"合成教学经验{index + 1}：来源记录需包含版本与适用范围；"
            "学生先独立说明理由，再两人交流；保留不同意见并明确哪些内容需要教师确认。"
            "课堂使用虚构示例，比较有来源的陈述与缺少依据的陈述，练习引用而非背诵。"
        )
        body = (paragraph * (1500 // len(paragraph) + 1))[:1500] if large else paragraph
        path = f"/v1/courses/{course_id}/teaching-memories"
        item = await api_post(
            client,
            path,
            {
                "kind": "experience",
                "title": f"合成经验 {index + 1}：来源与小组交流",
                "body": body,
                "source": {
                    "kind": "teacher_note",
                    "note": "验收脚本明确录入并确认的合成教师经验；不来自任何真实学生。",
                },
            },
        )
        items.append(
            await api_post(
                client, f"{path}/{item['id']}/confirm", {"content_hash": item["content_hash"]}
            )
        )
    return items


INSTRUCTIONS = {
    "assisted": (
        "这是合成验收。先 read_lesson_brief，再 search_teaching_memory(query='',limit=5)。"
        "尚缺组织方式，保存前必须且仅一次 ask_teacher 询问组织方式，等待真实工具回答；"
        "不能自行模拟回答。采用唯一的相关课程经验，按原样保留教师回答和来源 ID/hash。"
        "最后用 propose_assisted_lesson_plan 保存简洁的十分钟方案。"
    ),
    "research": (
        "这是合成验收。先 read_lesson_brief，再 search_teaching_memory(query='',limit=5)。"
        "采用全部五条经验，引用全部五条真实 ID/hash。结果外置后，按返回 reference.id "
        "调用 read_tool_result(offset=0,limit=4000)，依次用 next_offset 分页直到 complete=true；"
        "未读完不得凭 preview 猜测正文。读完后必须且仅一次 ask_teacher 询问组织方式，"
        "等待实际工具回答并原样保留。最后 propose_assisted_lesson_plan 保存简洁十分钟方案。"
    ),
    "background": (
        "这是合成后台调度验收。严格顺序：首先 spawn_lesson_review，请其审阅固定目标与时长；"
        "接着 read_lesson_brief；然后用返回 child_run_id 调用恰好一次 get_child_output；"
        "无论状态是否已完成，下一步必须 join_child，禁止重复轮询。join 返回后保留子任务"
        "完整 review 与真实 review_reference，最后 propose_background_lesson_plan 保存简洁"
        "十分钟方案，等待教师确认。不要声称已确认。"
    ),
}


async def create_run(client, course_id, name, cost_limit):
    return await api_post(
        client,
        f"/v1/courses/{course_id}/lesson-agent-runs",
        {
            "agent_profile": f"deepseek-{name}-lesson-agent-v1",
            "brief": BRIEF,
            "instructions": INSTRUCTIONS[name],
            "max_model_calls": 12,
            "max_tool_calls": 24,
            "max_context_characters": 40000,
            "max_seconds": 900,
            "max_total_tokens": 300000,
            "max_cost_usd": str(cost_limit),
        },
    )


async def drive(client, db, settings, run_id, *, can_answer):
    deadline, answers = monotonic() + 600, []
    while monotonic() < deadline:
        async with asyncio.timeout(max(0.1, deadline - monotonic())):
            await pool.run_pool(db, settings, concurrency=2, mode="drain", run_ids=(run_id,))
        detail = await api_get(client, f"/v1/agent-runs/{run_id}")
        if detail["status"] == "waiting_input":
            require(can_answer and not answers, "unexpected_repeated_teacher_question")
            question = detail["wait"].get("question")
            require(bool(question and question.get("prompt")), "missing_teacher_question")
            answers.append({"question": question, "synthetic_teacher_response": ANSWER})
            await api_post(
                client,
                f"/v1/agent-runs/{run_id}/wake",
                {"wait_token": detail["wait"]["token"], "response": ANSWER},
            )
        elif detail["status"] in {"waiting_teacher", "completed", "failed", "cancelled"}:
            require(detail["status"] == "waiting_teacher", "root_did_not_wait_for_teacher")
            return answers
        else:
            await asyncio.sleep(settings.poll_seconds)
    raise VerificationError("scenario_wall_clock_limit")


def row_view(row):
    # Only known synthetic Harness tables are passed here. Never serialize a
    # Teacher (token digest), Settings, client headers, or database connection.
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


def tool_receipts(run):
    names = {
        call["id"]: call["function"]["name"]
        for message in run.messages
        for call in message.get("tool_calls", [])
    }
    return [
        (names[message["tool_call_id"]], json.loads(message["content"]))
        for message in run.messages
        if message.get("role") == "tool"
    ]


async def capture(db, root_id):
    async with db.sessions() as session:
        root = await session.get(AgentRun, root_id)
        require(root is not None, "missing_root")
        runs = list(
            await session.scalars(
                select(AgentRun)
                .where(or_(AgentRun.id == root_id, AgentRun.root_run_id == root_id))
                .order_by(AgentRun.created_at, AgentRun.id)
            )
        )
        ids = [run.id for run in runs]
        result = {
            "runs": [row_view(run) for run in runs],
            "budget": await budgets.report(session, root),
        }
        for model, key in (
            (AgentEvent, "events"),
            (AgentArtifact, "artifacts"),
            (AgentContextCheckpoint, "context_checkpoints"),
            (AgentToolResult, "externalized_results"),
            (AgentModelReservation, "reservations"),
        ):
            rows = list(
                await session.scalars(
                    select(model).where(model.run_id.in_(ids)).order_by(model.created_at, model.id)
                )
            )
            result[key] = [row_view(row) for row in rows]
        return result


async def validate(db, client, root_id, name, memories, answers, guard):
    checks = []
    async with db.sessions() as session:
        root = await session.get(AgentRun, root_id)
        require(root.status == "waiting_teacher", "root_not_waiting_teacher")
        require(
            await catalog.resolve(root.task_type).artifact_exists(session, root), "invalid_artifact"
        )
        artifact = await session.scalar(
            select(AgentArtifact).where(AgentArtifact.run_id == root_id)
        )
        require(artifact.confirmed_at is None, "artifact_was_confirmed")
        require(artifact.content_hash == digest(artifact.body), "artifact_hash_mismatch")
        checks.append("valid_hashed_artifact_waiting_for_teacher_not_confirmed")
        receipts = tool_receipts(root)
        names = [item[0] for item in receipts]
        if name in {"assisted", "research"}:
            require(
                len(answers) == 1 and names.count("ask_teacher") == 1, "teacher_question_missing"
            )
            require(artifact.body["teacher_response"] == ANSWER, "teacher_response_changed")
            references = {
                (item["id"], item["content_hash"]) for item in artifact.body["memory_references"]
            }
            require(
                references == {(item["id"], item["content_hash"]) for item in memories},
                "incomplete_or_invalid_memory_references",
            )
            checks.append("explicit_teacher_question_answer_and_exact_confirmed_memory_references")
        if name == "research":
            saved = list(
                await session.scalars(
                    select(AgentToolResult).where(AgentToolResult.run_id == root_id)
                )
            )
            require(bool(saved), "large_result_not_externalized")
            for record in saved:
                parts, offset, complete = [], 0, False
                pages = [
                    output["result"]
                    for tool, output in receipts[: names.index("ask_teacher")]
                    if tool == "read_tool_result"
                    and output.get("ok")
                    and output["result"]["id"] == str(record.id)
                ]
                require(len(pages) >= 2, "result_not_read_in_multiple_pages")
                for page in pages:
                    require(page["offset"] == offset, "page_offset_not_contiguous")
                    require(page["content_hash"] == record.content_hash, "page_hash_mismatch")
                    require(not complete, "page_read_after_completion")
                    parts.append(page["text"])
                    offset, complete = page["next_offset"], page["complete"]
                require(complete, "incomplete_paginated_result")
                require(
                    "".join(parts) == tool_results.canonical_text(record.body), "page_body_mismatch"
                )
            checks.append("externalized_result_fully_read_in_order_before_teacher_question")
        if name == "background":
            require(
                names[:2] == ["spawn_lesson_review", "read_lesson_brief"], "background_order_wrong"
            )
            require(
                names.count("get_child_output") == names.count("join_child") == 1,
                "background_polled",
            )
            children = list(
                await session.scalars(select(AgentRun).where(AgentRun.parent_run_id == root_id))
            )
            require(len(children) == 1 and children[0].status == "completed", "child_not_completed")
            child_artifact = await session.scalar(
                select(AgentArtifact).where(AgentArtifact.run_id == children[0].id)
            )
            require(
                child_artifact is not None and child_artifact.confirmed_at is None,
                "child_confirmed",
            )
            require(
                artifact.body["review_reference"]
                == {
                    "child_run_id": str(children[0].id),
                    "artifact_id": str(child_artifact.id),
                    "content_hash": child_artifact.content_hash,
                }
                and artifact.body["review"] == child_artifact.body,
                "child_reference_mismatch",
            )
            checks.append("background_spawn_brief_single_status_join_and_unconfirmed_child")
        totals = await budgets.report(session, root)
        require(
            totals["calls"] > 0 and totals["unsettled_calls"] == totals["unpriced_calls"] == 0,
            "incomplete_usage_ledger",
        )
        require(
            totals["accounted_tokens"] <= root.limits["max_total_tokens"], "token_limit_exceeded"
        )
        require(
            Decimal(totals["accounted_cost_usd"]) <= Decimal(root.limits["max_cost_usd"]),
            "cost_limit_exceeded",
        )
        ids = {str(root_id)} | {
            str(value)
            for value in await session.scalars(
                select(AgentRun.id).where(AgentRun.root_run_id == root_id)
            )
        }
        calls = [item for item in guard.calls if item["run_id"] in ids]
        require(len(calls) == totals["calls"], "dispatch_ledger_count_mismatch")
        require(
            all(item.get("usage_complete") and item["status"] == "succeeded" for item in calls),
            "model_usage_not_complete",
        )
        require(
            all(item["metadata"].get("provider_request_id") for item in calls), "missing_request_id"
        )
        checks.append("all_real_replies_have_usage_request_ids_and_settled_token_cost_ledger")
    # Read the production inspection endpoints as well as database invariants.
    for suffix in ("artifact", "budget", "children", "context-checkpoints"):
        await api_get(client, f"/v1/agent-runs/{root_id}/{suffix}")
    after, sequences = 0, []
    for _ in range(100):
        page = await api_get(client, f"/v1/agent-runs/{root_id}/events?after={after}&limit=100")
        sequences.extend(item["sequence"] for item in page["items"])
        after = page["next_after"]
        if not page["has_more"]:
            break
    else:
        raise VerificationError("event_pagination_not_bounded")
    require(sequences == list(range(1, after + 1)), "event_stream_gap_or_duplicate")
    checks.append("production_artifact_budget_children_checkpoint_and_incremental_event_endpoints")
    cursor = max(0, after - 3)
    stream = await client.get(
        f"/v1/agent-runs/{root_id}/events/stream?follow_seconds=0&after={cursor}"
    )
    require(stream.status_code == 200, "sse_status_failed")
    identifiers = [line[4:] for line in stream.text.splitlines() if line.startswith("id: ")]
    require(
        identifiers == [f"{root_id}:{index}" for index in range(cursor + 1, after + 1)]
        and "event: end\n" in stream.text,
        "sse_missing_events_or_end",
    )
    resumed = await client.get(
        f"/v1/agent-runs/{root_id}/events/stream?follow_seconds=0",
        headers={"Last-Event-ID": f"{root_id}:{after}"},
    )
    require(
        resumed.status_code == 200
        and "event: end\n" in resumed.text
        and not any(line.startswith("id:") for line in resumed.text.splitlines()),
        "sse_reconnect_duplicated_events",
    )
    checks.append("real_run_sse_terminal_end_and_last_event_id_resume_without_duplicates")
    return checks


def write_report(directory, report, guard):
    report["global_model_guard"] = guard.snapshot()
    report["updated_at"] = datetime.now(UTC).isoformat()
    (directory / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    rows = [
        "# Real Harness acceptance",
        "",
        f"Outcome: {'PASS' if report.get('passed') else 'NOT PASSED'}",
        f"Model: {MODEL}; calls started: {len(guard.calls)} / {guard.max_calls}.",
        f"Accounted peak-price upper estimate: ${guard.accounted} / ${guard.max_cost_usd}.",
        "",
        "This is not a provider invoice. Unknown responses keep their full reservation.",
        "All inputs, teacher answers, course memories and learners are synthetic fixtures.",
        "The summary case seeds explicitly labelled synthetic history; only its summary and",
        "subsequent model responses are real API output. No proposed artifact is confirmed.",
        "",
        "| Scenario | Result | Root run |",
        "| --- | --- | --- |",
    ]
    for scenario in report["scenarios"]:
        rows.append(
            f"| {scenario['name']} | {scenario['status']} | {scenario.get('run_id', '-')} |"
        )
    rows += [
        "",
        f"Error code: {report.get('error', 'none')}",
        "",
        "Detailed synthetic transcripts, events, checkpoints, artifacts and budget ledgers are in report.json.",
    ]
    (directory / "report.md").write_text("\n".join(rows) + "\n", encoding="utf-8")


async def verify(args):
    load_dotenv(REPO / ".env")
    test_url = os.environ.get("TA_TEST_DATABASE_URL")
    require(bool(test_url), "test_database_url_required")
    parsed = make_url(test_url)
    require(
        parsed.drivername == "postgresql+asyncpg" and (parsed.database or "").endswith("_test"),
        "isolated_test_database_required",
    )
    settings = Settings(
        _env_file=REPO / ".env",
        database_url=test_url,
        dev_api_token="synthetic-harness-" + secrets.token_urlsafe(32),
        deepseek_model=MODEL,
        deepseek_expected_model=MODEL,
        deepseek_max_output_tokens=2048,
        deepseek_input_usd_per_million=INPUT_PRICE,
        deepseek_output_usd_per_million=OUTPUT_PRICE,
        deepseek_cached_input_usd_per_million=CACHE_PRICE,
        deepseek_price_version=PRICE_VERSION,
        deepseek_price_source=budgets.PRICE_SOURCE,
        call_timeout_seconds=30,
        lease_seconds=60,
        poll_seconds=0.1,
        harness_hook_id="deny-tools",
        harness_hook_config={},
    )
    require(bool(settings.deepseek_api_key), "deepseek_key_required")
    endpoint = urlsplit(settings.deepseek_endpoint)
    require(
        endpoint.scheme == "https"
        and endpoint.hostname == "api.deepseek.com"
        and endpoint.port in {None, 443},
        "official_deepseek_endpoint_required",
    )
    from live_summary_case import run_summary_case

    run_uuid = uuid4()
    directory = args.output / str(run_uuid)
    directory.mkdir(parents=True, exist_ok=False)
    guard = CallGuard(args.max_calls, args.max_cost_usd)
    worker_class = guarded_worker_class(guard)
    per_case = (args.max_cost_usd / 4).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
    report = {
        "verification_id": str(run_uuid),
        "passed": False,
        "synthetic_inputs_only": True,
        "summary_history_is_seeded_synthetic_fixture": True,
        "model": MODEL,
        "endpoint": settings.deepseek_endpoint,
        "pricing": {
            "version": PRICE_VERSION,
            "source": budgets.PRICE_SOURCE,
            "input": str(INPUT_PRICE),
            "output": str(OUTPUT_PRICE),
            "cached_input": str(CACHE_PRICE),
            "unit": "USD per million tokens; fixed peak ceiling, not invoice",
        },
        "per_root_limits": {
            "max_model_calls": 12,
            "max_tool_calls": 24,
            "max_total_tokens": 300000,
            "max_cost_usd": str(per_case),
        },
        "scenarios": [],
        "cleanup": {"schema_dropped": False, "worker_tasks_closed": False},
    }
    schema = "real_harness_" + run_uuid.hex
    require(bool(re.fullmatch(r"real_harness_[a-f0-9]{32}", schema)), "invalid_generated_schema")
    admin, db, created = create_async_engine(test_url), None, False
    write_report(directory, report, guard)
    try:
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            created = True
        db = Database(test_url, schema=schema)
        async with db.engine.begin() as connection:

            def migrate(sync_connection):
                config = Config(str(REPO / "alembic.ini"))
                config.attributes["connection"] = sync_connection
                command.upgrade(config, "head")
                command.check(config)

            await connection.run_sync(migrate)
        await bootstrap(db, settings, name="Synthetic Harness acceptance teacher")
        app = create_app(settings, db)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://synthetic-harness-test",
            headers={"Authorization": f"Bearer {settings.dev_api_token}"},
        ) as client:
            with patch.object(pool, "HarnessWorker", worker_class):
                for name in ("assisted", "research", "background", "summary"):
                    case = {"name": name, "status": "running"}
                    report["scenarios"].append(case)
                    write_report(directory, report, guard)
                    print(
                        json.dumps(
                            {"scenario": name, "status": "running", "calls": len(guard.calls)}
                        ),
                        flush=True,
                    )
                    course_id = await create_course(client, name)
                    if name == "summary":
                        case["seeded_synthetic_history"] = True
                        async with asyncio.timeout(600):
                            root_id = await run_summary_case(
                                client, db, settings, worker_class, UUID(course_id), per_case
                            )
                        case["run_id"] = str(root_id)
                        case["checks"] = await validate(db, client, root_id, name, [], [], guard)
                    else:
                        memories = (
                            await create_memories(
                                client,
                                course_id,
                                5 if name == "research" else 1,
                                large=name == "research",
                            )
                            if name != "background"
                            else []
                        )
                        case["confirmed_synthetic_memories"] = memories
                        created_run = await create_run(client, course_id, name, per_case)
                        root_id = UUID(created_run["id"])
                        case["run_id"] = str(root_id)
                        answers = await drive(
                            client, db, settings, root_id, can_answer=name != "background"
                        )
                        case["teacher_answers"] = answers
                        case["checks"] = await validate(
                            db, client, root_id, name, memories, answers, guard
                        )
                    case["evidence"] = await capture(db, root_id)
                    case["status"] = "passed"
                    write_report(directory, report, guard)
                    print(
                        json.dumps(
                            {"scenario": name, "status": "passed", "calls": len(guard.calls)}
                        ),
                        flush=True,
                    )
        report["passed"] = len(report["scenarios"]) == 4 and not guard.denials
    except (Exception, asyncio.CancelledError) as exc:
        report["error"] = safe_error(exc)
        if report["scenarios"] and report["scenarios"][-1]["status"] == "running":
            report["scenarios"][-1]["status"] = "failed"
    finally:
        # run_pool and HarnessWorker.invoke close/cancel their own tasks. Cancel
        # any remaining script-created tasks before collecting final DB state.
        pending = [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task() and not task.done()
        ]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        report["cleanup"]["worker_tasks_closed"] = True
        if db is not None:
            try:
                async with db.sessions() as session:
                    root_ids = list(
                        await session.scalars(
                            select(AgentRun.id).where(AgentRun.parent_run_id.is_(None))
                        )
                    )
                known = {item.get("run_id") for item in report["scenarios"]}
                for root_id in root_ids:
                    if str(root_id) not in known:
                        # A helper can fail after creation but before returning
                        # its run id. Preserve that failed root as well.
                        report["scenarios"].append(
                            {
                                "name": "incomplete_helper_run",
                                "status": "failed",
                                "run_id": str(root_id),
                            }
                        )
                for case in report["scenarios"]:
                    if case.get("run_id"):
                        case["evidence"] = await capture(db, UUID(case["run_id"]))
            except Exception as exc:
                report["capture_error"] = safe_error(exc)
                report["passed"] = False
        # Attempt persistence before schema deletion even on failures. A disk
        # error must not prevent closing workers and dropping the test schema.
        try:
            write_report(directory, report, guard)
        except OSError:
            report["report_write_error"] = "report_write_failed"
            report["passed"] = False
        try:
            if db is not None:
                await db.close()
        except Exception as exc:
            report["database_close_error"] = safe_error(exc)
            report["passed"] = False
        try:
            if created:
                async with admin.begin() as connection:
                    await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
                report["cleanup"]["schema_dropped"] = True
        except Exception as exc:
            report["cleanup_error"] = safe_error(exc)
            report["passed"] = False
        finally:
            await admin.dispose()
            try:
                write_report(directory, report, guard)
            except OSError:
                report["report_write_error"] = "report_write_failed"
                report["passed"] = False
    print(
        json.dumps(
            {
                "passed": report["passed"],
                "verification_id": str(run_uuid),
                "report_directory": str(directory),
                "calls": len(guard.calls),
                "accounted_peak_cost_usd": str(guard.accounted),
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["passed"] else 1


def call_limit(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("max-calls must be an integer from 1 to 48") from None
    if not 1 <= number <= 48:
        raise argparse.ArgumentTypeError("max-calls must be an integer from 1 to 48")
    return number


def cost_limit(value):
    try:
        amount = Decimal(value)
        if not amount.is_finite() or not Decimal("0.01") <= amount <= Decimal("1.00"):
            raise ValueError
    except (ValueError, ArithmeticError):
        raise argparse.ArgumentTypeError("max-cost-usd must be from 0.01 to 1.00") from None
    return amount


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--max-calls", type=call_limit, default=36, help="Global real-call cap, 1..48 (default: 36)"
    )
    parser.add_argument(
        "--max-cost-usd",
        type=cost_limit,
        default=Decimal("0.30"),
        help="Global peak-price accounting ceiling, USD 0.01..1.00 (default: 0.30); quarter per scenario",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=REPO / ".local" / "e2e" / "harness-v5-real",
        help="Report parent directory; each execution creates a new UUID directory",
    )
    args = parser.parse_args()
    try:
        return asyncio.run(verify(args))
    except (Exception, KeyboardInterrupt) as exc:
        print(json.dumps({"passed": False, "error": safe_error(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

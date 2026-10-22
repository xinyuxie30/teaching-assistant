"""Externalized result receipts preserve exact data and constrain paged recall."""

import json
from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from conftest import post
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from teaching_assistant.db import AgentRun, AgentToolResult, Teacher
from teaching_assistant.domain import DomainError, digest
from teaching_assistant.harness import tool_results as storage
from teaching_assistant.harness.context import tool_result
from teaching_assistant.harness.contracts import ToolCall

pytestmark = pytest.mark.postgres

CONFIG = {
    "version": storage.STORAGE_VERSION,
    "allowed_tools": ["search_teaching_memory"],
    "inline_threshold_characters": 512,
    "preview_characters": 64,
    "read_page_characters": 1024,
}
BODY = {"records": [{"text": "完整资料😀\n不可丢失。" * 400, "source": "teacher-confirmed"}]}


@pytest.fixture
async def course(client):
    return (
        await post(
            client,
            "/v1/workspaces",
            {
                "course_name": "Stored tool reference course",
                "classroom_name": "Synthetic class",
                "students": [{"external_id": "r1", "name": "Synthetic student"}],
            },
        )
    )["course_id"]


async def stored(
    db,
    client,
    course,
    *,
    body=BODY,
    name="search_teaching_memory",
    runtime="harness-runtime-v5",
    failure=False,
):
    created = await post(
        client,
        f"/v1/courses/{course}/lesson-agent-runs",
        {"brief": {"topic": "可追溯性", "objectives": ["保留资料来源"], "duration_minutes": 10}},
    )
    call = ToolCall.model_validate(
        {"id": "original-call", "function": {"name": name, "arguments": "{}"}}
    )
    async with db.sessions.begin() as session:
        run = await session.get(AgentRun, UUID(created["id"]))
        run.manifest = {
            **run.manifest,
            "runtime_version": runtime,
            "tool_result_storage": deepcopy(CONFIG),
        }
        run.messages = [
            *run.messages,
            {"role": "assistant", "content": None, "tool_calls": [call.model_dump(mode="json")]},
        ]
        run.pending_calls = [call.model_dump(mode="json")]
        reference = None if failure else await storage.persist_receipt(session, run, call, body)
        output = (
            {"ok": False, "error_code": "fixture", "detail": "error" * 1000}
            if failure
            else {"ok": True, "result": body}
        )
        run.messages = [*run.messages, tool_result(call.id, output)]
        run.pending_calls = []
    return UUID(created["id"]), call, reference


async def test_large_single_field_remains_exact_and_projection_is_restart_stable(
    db, client, course
):
    run_id, call, reference = await stored(db, client, course)
    assert len(reference["preview"]) == CONFIG["preview_characters"]
    assert reference["content_hash"] == digest(BODY)
    async with db.sessions.begin() as session:
        run = await session.get(AgentRun, run_id)
        original = deepcopy(run.messages)
        projected = await storage.project_messages(session, run)
        assert run.messages == original
        assert projected.source_hash == digest(original)
        assert projected.projection_hash != projected.source_hash
        assert projected.references == [reference]
        result = json.loads(projected.messages[-1]["content"])["result"]
        assert result["externalized"] and result["reference"] == reference
        assert projected.messages[-1]["tool_call_id"] == call.id
        assert projected.messages[-2] == original[-2]
        assert len(projected.messages[-1]["content"]) < 1200
        assert await storage.persist_receipt(session, run, call, deepcopy(BODY)) == reference
    async with db.sessions() as session:
        run = await session.get(AgentRun, run_id)
        assert await storage.project_messages(session, run) == projected
        assert run.messages == original
        record = await session.scalar(
            select(AgentToolResult).where(AgentToolResult.run_id == run_id)
        )
        assert record.body == BODY
        assert await session.scalar(select(func.count()).select_from(AgentToolResult)) == 1


async def test_paged_read_reconstructs_exact_unicode_json_and_handles_eof(db, client, course):
    run_id, _, reference = await stored(db, client, course)
    async with db.sessions() as session:
        run = await session.get(AgentRun, run_id)
        offset, parts = 0, []
        while True:
            page = await storage.read_tool_result(
                session,
                run,
                storage.ReadToolResultInput(id=reference["id"], offset=offset, limit=731),
            )
            storage.ReadToolResultOutput.model_validate(page)
            assert page["offset"] == offset and page["content_hash"] == digest(BODY)
            parts.append(page["text"])
            if page["complete"]:
                assert page["next_offset"] is None
                break
            offset = page["next_offset"]
        text = "".join(parts)
        assert text == storage.canonical_text(BODY) and json.loads(text) == BODY
        eof = await storage.read_tool_result(
            session, run, storage.ReadToolResultInput(id=reference["id"], offset=len(text), limit=1)
        )
        assert eof["complete"] and eof["text"] == ""
        with pytest.raises(DomainError, match="Offset exceeds"):
            await storage.read_tool_result(
                session,
                run,
                storage.ReadToolResultInput(id=reference["id"], offset=len(text) + 1, limit=1),
            )
        with pytest.raises(DomainError, match="page exceeds"):
            await storage.read_tool_result(
                session, run, storage.ReadToolResultInput(id=reference["id"], limit=1025)
            )


@pytest.mark.parametrize(
    "runtime,name,body,failure",
    [
        ("harness-runtime-v4", "search_teaching_memory", BODY, False),
        ("harness-runtime-v5", "read_question", BODY, False),
        ("harness-runtime-v5", "read_lesson_brief", BODY, False),
        ("harness-runtime-v5", "propose_lesson_plan", BODY, False),
        ("harness-runtime-v5", "read_tool_result", BODY, False),
        ("harness-runtime-v5", "search_teaching_memory", {"small": "fact"}, False),
        ("harness-runtime-v5", "search_teaching_memory", BODY, True),
    ],
)
async def test_protected_small_failed_and_legacy_results_remain_inline(
    db, client, course, runtime, name, body, failure
):
    run_id, _, reference = await stored(
        db, client, course, body=body, name=name, runtime=runtime, failure=failure
    )
    assert reference is None
    async with db.sessions() as session:
        run = await session.get(AgentRun, run_id)
        projection = await storage.project_messages(session, run)
        assert projection.messages == run.messages and projection.references == []
        assert projection.source_hash == projection.projection_hash
        assert await session.scalar(select(func.count()).select_from(AgentToolResult)) == 0


async def test_read_is_bound_to_run_and_teacher_even_with_known_result_id(db, client, course):
    run_id, _, reference = await stored(db, client, course)
    other_run_id, _, _ = await stored(db, client, course)
    async with db.sessions() as session:
        other_run = await session.get(AgentRun, other_run_id)
        with pytest.raises(DomainError) as denied:
            await storage.read_tool_result(
                session, other_run, storage.ReadToolResultInput(id=reference["id"], limit=1)
            )
        assert denied.value.status == 404
        run = await session.get(AgentRun, run_id)
        # The actor/run binding cannot be changed by possession of a reference.
        run.teacher_id = uuid4()
        with session.no_autoflush:
            with pytest.raises(DomainError) as denied:
                await storage.read_tool_result(
                    session, run, storage.ReadToolResultInput(id=reference["id"], limit=1)
                )
        assert denied.value.status == 404


async def test_same_call_cannot_be_rebound_even_to_a_smaller_result(db, client, course):
    run_id, call, _ = await stored(db, client, course)
    async with db.sessions.begin() as session:
        run = await session.get(AgentRun, run_id)
        with pytest.raises(DomainError, match="integrity"):
            await storage.persist_receipt(session, run, call, {"smaller": "changed"})


async def test_projection_rejects_changed_original_and_missing_saved_reference(db, client, course):
    run_id, _, _ = await stored(db, client, course)
    async with db.sessions() as session:
        run = await session.get(AgentRun, run_id)
        records = list(
            await session.scalars(select(AgentToolResult).where(AgentToolResult.run_id == run_id))
        )
        with pytest.raises(DomainError) as missing:
            storage.project(
                run.messages, [], run.manifest, run_id=run.id, teacher_id=run.teacher_id
            )
        assert missing.value.code == "tool_result_missing"
        changed = deepcopy(run.messages)
        changed[-1] = tool_result(
            "original-call", {"ok": True, "result": {"records": "changed" * 1000}}
        )
        with pytest.raises(DomainError, match="integrity"):
            storage.project(
                changed, records, run.manifest, run_id=run.id, teacher_id=run.teacher_id
            )
        smaller = deepcopy(run.messages)
        smaller[-1] = tool_result("original-call", {"ok": True, "result": {"changed": "small"}})
        with pytest.raises(DomainError, match="integrity"):
            storage.project(
                smaller, records, run.manifest, run_id=run.id, teacher_id=run.teacher_id
            )
        changed = deepcopy(run.messages)
        changed[-2]["tool_calls"][0]["function"]["arguments"] = '{"query":"changed"}'
        with pytest.raises(DomainError, match="integrity"):
            storage.project(
                changed, records, run.manifest, run_id=run.id, teacher_id=run.teacher_id
            )


@pytest.mark.parametrize(
    "field,value",
    [("body", {"changed": True}), ("content_hash", "0" * 64), ("tool_call_id", "other-call")],
)
async def test_result_receipts_are_immutable_in_database(db, client, course, field, value):
    _, _, reference = await stored(db, client, course)
    with pytest.raises(DBAPIError):
        async with db.sessions.begin() as session:
            record = await session.get(AgentToolResult, UUID(reference["id"]))
            setattr(record, field, value)
    with pytest.raises(DBAPIError):
        async with db.sessions.begin() as session:
            await session.delete(await session.get(AgentToolResult, UUID(reference["id"])))


async def test_database_rejects_cross_teacher_tool_result_insert(db, client, course):
    run_id, _, _ = await stored(db, client, course)
    async with db.sessions.begin() as session:
        other = Teacher(name="Synthetic other teacher", token_digest="9" * 64)
        session.add(other)
        await session.flush()
        other_id = other.id
    with pytest.raises(DBAPIError):
        async with db.sessions.begin() as session:
            session.add(
                AgentToolResult(
                    run_id=run_id,
                    teacher_id=other_id,
                    tool_call_id="cross-teacher",
                    tool_name="search_teaching_memory",
                    arguments_hash=digest("{}"),
                    body=BODY,
                    content_hash=digest(BODY),
                    character_count=len(storage.canonical_text(BODY)),
                )
            )


def test_policy_and_read_arguments_cannot_enable_protected_tools_or_unbounded_reads():
    for name in ("read_question", "read_lesson_brief", "read_tool_result", "propose_lesson_plan"):
        with pytest.raises(DomainError):
            storage.configuration(
                {
                    "runtime_version": "harness-runtime-v5",
                    "tool_result_storage": {**CONFIG, "allowed_tools": [name]},
                }
            )
    for extra in (
        {"teacher_id": str(uuid4())},
        {"run_id": str(uuid4())},
        {"limit": 8001},
        {"offset": -1},
        {"limit": True},
    ):
        with pytest.raises(ValidationError):
            storage.ReadToolResultInput(id=uuid4(), **extra)


async def test_tool_result_migration_empty_round_trip_and_metadata_match(db):
    def check(connection):
        config = Config("alembic.ini")
        config.attributes["connection"] = connection
        command.downgrade(config, "0010")
        command.upgrade(config, "head")
        command.check(config)

    async with db.engine.begin() as connection:
        await connection.run_sync(check)


async def test_tool_result_migration_refuses_to_discard_history(db, client, course):
    await stored(db, client, course)

    def downgrade(connection):
        config = Config("alembic.ini")
        config.attributes["connection"] = connection
        command.downgrade(config, "0010")

    with pytest.raises(DBAPIError):
        async with db.engine.begin() as connection:
            await connection.run_sync(downgrade)

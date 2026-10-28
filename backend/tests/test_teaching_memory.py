"""Knowledge provenance, explicit teacher decisions, and scoped bounded recall."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from alembic import command
from alembic.config import Config
from conftest import post
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from teaching_assistant import teaching_memory as memory
from teaching_assistant.api import create_app
from teaching_assistant.cli import bootstrap
from teaching_assistant.db import Audit, TeachingMemory
from teaching_assistant.harness import catalog
from teaching_assistant.harness.worker import HarnessWorker

pytestmark = pytest.mark.postgres

BRIEF = {"topic": "版本记录", "objectives": ["解释版本记录"], "duration_minutes": 10}


async def create_course(client):
    return (
        await post(
            client,
            "/v1/workspaces",
            {
                "course_name": "Memory course",
                "classroom_name": "Memory class",
                "students": [{"external_id": "m1", "name": "Synthetic student"}],
            },
        )
    )["course_id"]


@pytest.fixture
async def course(client):
    return await create_course(client)


def path(course, item=None, suffix=""):
    return f"/v1/courses/{course}/teaching-memories" + (f"/{item['id']}{suffix}" if item else "")


def payload(**options):
    return {
        "kind": "preference",
        "title": "课堂组织",
        "body": "练习后请学生解释理由。",
        "source": {"kind": "teacher_note", "note": "本课程教师确认的课堂组织经验"},
        **options,
    }


async def save(client, course, *, confirm=True, **options):
    item = await post(client, path(course), payload(**options))
    if confirm:
        item = await post(
            client, path(course, item, "/confirm"), {"content_hash": item["content_hash"]}
        )
    return item


async def recall(client, course, **params):
    response = await client.get(path(course), params=params)
    assert response.status_code == 200, response.text
    result = response.json()
    memory.MemorySearchResult.model_validate(result)
    return result["items"]


async def test_memory_requires_confirmation_and_keeps_idempotent_audit(db, client, course):
    candidate = await post(client, path(course), payload(), key="memory-create")
    assert await post(client, path(course), payload(), key="memory-create") == candidate
    assert candidate["confirmed_at"] is None and await recall(client, course) == []
    await post(client, path(course), payload(body="另一份正文"), key="memory-create", expected=409)
    await post(
        client, path(course, candidate, "/confirm"), {"content_hash": "0" * 64}, expected=409
    )
    result = await post(
        client,
        path(course, candidate, "/confirm"),
        {"content_hash": candidate["content_hash"]},
        key="memory-confirm",
    )
    assert (
        await post(
            client,
            path(course, candidate, "/confirm"),
            {"content_hash": candidate["content_hash"]},
            key="memory-confirm",
        )
        == result
    )
    assert result["confirmed_at"] and result["content_hash"] == candidate["content_hash"]
    assert await recall(client, course, query="理由") == [result]
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(TeachingMemory)) == 1
        events = list(
            await session.scalars(
                select(Audit.action).where(Audit.object_id == UUID(candidate["id"]))
            )
        )
        assert sorted(events) == ["teaching_memory_confirmed", "teaching_memory_proposed"]


async def test_memory_recall_is_literal_bounded_and_excludes_drafts_future_revoked(client, course):
    await save(client, course, confirm=False, title="未确认")
    await save(
        client,
        course,
        title="未来生效",
        valid_from=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
    )
    revoked = await save(client, course, title="撤销知识")
    revoked_result = await post(
        client,
        path(course, revoked, "/revoke"),
        {"content_hash": revoked["content_hash"], "reason": "本课程不再适用"},
        key="revoke",
    )
    assert revoked_result["revoked_at"]
    assert (
        await post(
            client,
            path(course, revoked, "/revoke"),
            {"content_hash": revoked["content_hash"], "reason": "本课程不再适用"},
            key="revoke",
        )
        == revoked_result
    )
    await post(
        client,
        path(course, revoked, "/confirm"),
        {"content_hash": revoked["content_hash"]},
        expected=409,
    )
    literal = await save(
        client, course, title="100%_保留", body="Only literal punctuation matches."
    )
    for i in range(6):
        await save(client, course, title=f"普通经验 {i}")
    assert len(await recall(client, course)) == 5
    assert await recall(client, course, query="%_") == [literal]
    assert await recall(client, course, query="' OR 1=1 --") == []
    assert len(await recall(client, course, limit=1)) == 1
    assert (await client.get(path(course), params={"limit": 6})).status_code == 422
    assert (await client.get(path(course), params={"query": "x" * 201})).status_code == 422


async def test_memory_replacement_is_explicit_and_old_history_survives(client, course):
    old = await save(client, course)
    replacement = await save(
        client,
        course,
        confirm=False,
        supersedes_id=old["id"],
        body="练习后先同伴交流，再解释理由。",
    )
    competing = await save(
        client, course, confirm=False, supersedes_id=old["id"], body="另一项经验"
    )
    assert await recall(client, course) == [old]
    newer = await post(
        client, path(course, replacement, "/confirm"), {"content_hash": replacement["content_hash"]}
    )
    assert await recall(client, course) == [newer]
    historic = (await client.get(path(course, old))).json()
    assert historic["body"] == old["body"] and historic["content_hash"] == old["content_hash"]
    assert historic["revoked_at"]
    await post(
        client,
        path(course, competing, "/confirm"),
        {"content_hash": competing["content_hash"]},
        expected=409,
    )


async def test_memory_scope_is_rechecked_for_every_read_and_write(db, settings, client, course):
    own = await save(client, course)
    other_course = await create_course(client)
    assert await recall(client, other_course) == []
    assert (await client.get(path(other_course, own))).status_code == 404
    await save_foreign_lineage(client, other_course, own)
    other_settings = settings.model_copy(update={"dev_api_token": "memory-other-" + uuid4().hex})
    await bootstrap(db, other_settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(other_settings, db)),
        base_url="http://test",
        headers={"Authorization": f"Bearer {other_settings.dev_api_token}"},
    ) as other:
        for url in (path(course), path(course, own)):
            assert (await other.get(url)).status_code == 404
        await post(other, path(course), payload(), expected=404)
        await post(
            other,
            path(course, own, "/confirm"),
            {"content_hash": own["content_hash"]},
            expected=404,
        )
        await post(
            other,
            path(course, own, "/revoke"),
            {"content_hash": own["content_hash"], "reason": "invalid"},
            expected=404,
        )


async def save_foreign_lineage(client, other_course, own):
    await post(client, path(other_course), payload(supersedes_id=own["id"]), expected=404)


async def test_memory_artifact_source_requires_confirmed_matching_course(
    db, settings, client, course
):
    run = await post(client, f"/v1/courses/{course}/lesson-agent-runs", {"brief": BRIEF})
    await HarnessWorker(db, settings, run_ids=(UUID(run["id"]),)).run_once()
    artifact = (await client.get(f"/v1/agent-runs/{run['id']}/artifact")).json()
    source = {
        "kind": "confirmed_agent_artifact",
        "note": "从教师确认的教学方案提炼",
        "artifact_id": artifact["id"],
    }
    await post(client, path(course), payload(source=source), expected=409)
    await post(
        client, f"/v1/agent-runs/{run['id']}/confirm", {"content_hash": artifact["content_hash"]}
    )
    candidate = await save(client, course, source=source, kind="experience")
    assert candidate["source"]["content_hash"] == artifact["content_hash"]
    assert await recall(client, course) == [candidate]
    second_course = await create_course(client)
    await post(client, path(second_course), payload(source=source), expected=404)


async def test_memory_expiry_is_applied_at_recall_and_confirmation(client, course):
    expiry = (datetime.now(UTC) + timedelta(seconds=1.5)).isoformat()
    active = await save(client, course, valid_until=expiry)
    draft = await save(client, course, confirm=False, valid_until=expiry)
    assert await recall(client, course) == [active]
    await asyncio.sleep(1.6)
    assert await recall(client, course) == []
    await post(
        client,
        path(course, draft, "/confirm"),
        {"content_hash": draft["content_hash"]},
        expected=409,
    )
    await post(client, path(course), payload(valid_until=expiry), expected=422)


@pytest.mark.parametrize(
    "field,value",
    [("body", "changed"), ("source", {}), ("content_hash", "0" * 64), ("teacher_id", None)],
)
async def test_memory_fixed_content_is_immutable_in_database(db, client, course, field, value):
    item = await save(client, course, confirm=False)
    with pytest.raises(DBAPIError):
        async with db.sessions.begin() as session:
            row = await session.get(TeachingMemory, UUID(item["id"]))
            setattr(row, field, value)
    assert (await client.get(path(course, item))).json() == item


async def test_memory_terminal_decisions_and_history_cannot_be_erased(db, client, course):
    item = await save(client, course)
    await post(
        client,
        path(course, item, "/revoke"),
        {"content_hash": item["content_hash"], "reason": "expired practice"},
    )
    for field in ("confirmed_at", "revoked_at"):
        with pytest.raises(DBAPIError):
            async with db.sessions.begin() as session:
                row = await session.get(TeachingMemory, UUID(item["id"]))
                setattr(row, field, None)
    with pytest.raises(DBAPIError):
        async with db.sessions.begin() as session:
            await session.delete(await session.get(TeachingMemory, UUID(item["id"])))


async def test_memory_query_contract_cannot_request_scope_or_confirmation(
    db, settings, client, course
):
    for extras in ({"teacher_id": str(uuid4())}, {"course_id": course}, {"confirmed": True}):
        with pytest.raises(ValidationError):
            memory.MemorySearchInput.model_validate(extras)
    with pytest.raises(ValidationError):
        memory.MemorySourceInput(kind="teacher_note", note="explicit", artifact_id=uuid4())
    for task_type in ("rubric", "lesson-plan", "lesson-plan-team", "lesson-practice"):
        names = {entry["name"] for entry in catalog.resolve(task_type).registry.manifest()}
        assert names.isdisjoint(
            {
                "confirm_teaching_memory",
                "create_teaching_memory",
                "revoke_teaching_memory",
                "search_teaching_memory",
            }
        )
    await post(
        client, path(course), payload(confirmed_at=datetime.now(UTC).isoformat()), expected=422
    )


async def test_memory_migration_empty_round_trip_and_model_consistency(db):
    def check(connection):
        config = Config("alembic.ini")
        config.attributes["connection"] = connection
        command.downgrade(config, "0009")
        command.upgrade(config, "head")
        command.check(config)

    async with db.engine.begin() as connection:
        await connection.run_sync(check)


async def test_memory_migration_downgrade_refuses_history(db, client, course):
    await save(client, course, confirm=False)

    def downgrade(connection):
        config = Config("alembic.ini")
        config.attributes["connection"] = connection
        command.downgrade(config, "0009")

    with pytest.raises(DBAPIError):
        async with db.engine.begin() as connection:
            await connection.run_sync(downgrade)
    async with db.sessions() as session:
        assert await session.scalar(text("SELECT version_num FROM alembic_version")) == "0013"

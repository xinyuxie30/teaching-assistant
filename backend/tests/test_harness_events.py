"""Bounded event replay is authorized on every page without model execution."""

import asyncio
import json
from uuid import UUID, uuid4

import httpx
import pytest
from conftest import post
from sqlalchemy import select
from teaching_assistant.api import create_app
from teaching_assistant.cli import bootstrap, rotate_token
from teaching_assistant.db import AgentRun, Course, Teacher
from teaching_assistant.domain import text_digest
from teaching_assistant.harness import events
from teaching_assistant.harness.store import event

pytestmark = pytest.mark.postgres


@pytest.fixture
async def run(client):
    workspace = await post(
        client,
        "/v1/workspaces",
        {
            "course_name": "Event replay course",
            "classroom_name": "Synthetic class",
            "students": [{"external_id": "event-1", "name": "Synthetic student"}],
        },
    )
    created = await post(
        client,
        f"/v1/courses/{workspace['course_id']}/lesson-agent-runs",
        {"brief": {"topic": "持久事件", "objectives": ["解释可恢复任务"], "duration_minutes": 10}},
    )
    return created


async def append_events(db, run_id, count, *, label="fixture"):
    async with db.sessions.begin() as session:
        row = await session.get(AgentRun, UUID(str(run_id)))
        for index in range(count):
            event(session, row, label, index=index)


def parse_stream(text):
    result = []
    for block in text.split("\n\n"):
        if not block or block.startswith(":"):
            continue
        fields = dict(line.split(": ", 1) for line in block.splitlines())
        fields["data"] = json.loads(fields["data"])
        result.append(fields)
    return result


async def get_stream(client, run, **options):
    response = await client.get(
        f"/v1/agent-runs/{run['id']}/events/stream", params={"follow_seconds": 0, **options}
    )
    assert response.status_code == 200, response.text
    return response, parse_stream(response.text)


async def test_sse_run_scoped_cursor_and_after_resume_without_duplicates(db, client, run):
    await append_events(db, run["id"], 4)
    response = await client.get(
        f"/v1/agent-runs/{run['id']}/events/stream",
        params={"after": 1, "follow_seconds": 0},
        headers={"Last-Event-ID": f"{run['id']}:3"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-store"
    frames = parse_stream(response.text)
    assert [entry["data"]["sequence"] for entry in frames if entry["event"] == "agent_event"] == [
        4,
        5,
    ]
    assert frames[-1] == {"event": "reconnect", "data": {"after": 5}}
    for cursor in (
        f"{uuid4()}:3",
        f"{run['id']}:-1",
        f"{run['id']}:3.0",
        f"{run['id']}:999999999999999999",
        "not-a-cursor",
    ):
        invalid = await client.get(
            f"/v1/agent-runs/{run['id']}/events/stream",
            params={"follow_seconds": 0},
            headers={"Last-Event-ID": cursor},
        )
        assert invalid.status_code == 422 and invalid.json()["code"] == "invalid_event_cursor"
    invalid = await client.get(
        f"/v1/agent-runs/{run['id']}/events/stream",
        params={"after": 999999999999999999, "follow_seconds": 0},
    )
    assert invalid.status_code == 422


async def test_sse_deadline_limits_a_full_page_and_reconnections_drain_backlog(db, client, run):
    await append_events(db, run["id"], 204)
    cursor, seen = 0, []
    for expected_count in (100, 100, 5):
        _, frames = await get_stream(client, run, after=cursor)
        rows = [entry["data"] for entry in frames if entry["event"] == "agent_event"]
        assert len(rows) == expected_count
        assert frames[-1]["event"] == "reconnect"
        cursor = frames[-1]["data"]["after"]
        seen.extend(row["sequence"] for row in rows)
    assert seen == list(range(1, 206)) and cursor == 205
    page = (
        await client.get(f"/v1/agent-runs/{run['id']}/events", params={"after": 100, "limit": 100})
    ).json()
    assert [row["sequence"] for row in page["items"]] == list(range(101, 201))
    assert page["has_more"] and page["next_after"] == 200
    assert (
        await client.get(f"/v1/agent-runs/{run['id']}/events", params={"limit": 101})
    ).status_code == 422


async def test_sse_terminal_end_follows_final_durable_event_and_never_calls_model(db, client, run):
    cancelled = await post(client, f"/v1/agent-runs/{run['id']}/cancel", {})
    _, frames = await get_stream(client, run)
    assert frames[-1] == {
        "event": "end",
        "data": {"status": "cancelled", "after": cancelled["event_sequence"]},
    }
    assert frames[-2]["data"]["kind"] == "cancelled"
    _, resumed = await get_stream(client, run, after=cancelled["event_sequence"])
    assert resumed == [frames[-1]]
    async with db.sessions() as session:
        row = await session.get(AgentRun, UUID(run["id"]))
        assert row.models_used == row.tools_used == row.tree_models_used == row.tree_tools_used == 0


async def test_follow_yields_heartbeat_then_reconnect_without_mutating_run(
    db, settings, client, run
):
    async with db.sessions() as session:
        row = await session.get(AgentRun, UUID(run["id"]))
        teacher_id = row.teacher_id
    frames = [
        entry
        async for entry in events.stream_events(
            db,
            teacher_id,
            UUID(run["id"]),
            1,
            token_digest=text_digest(settings.dev_api_token),
            duration=0.03,
            poll=0.005,
        )
    ]
    assert ": heartbeat\n\n" in frames
    assert parse_stream("".join(frames)) == [{"event": "reconnect", "data": {"after": 1}}]
    async with db.sessions() as session:
        row = await session.get(AgentRun, UUID(run["id"]))
        assert row.status == "queued" and row.models_used == row.tools_used == 0


async def test_event_reads_reject_other_teacher_and_revoked_course_scope(db, settings, client, run):
    other_settings = settings.model_copy(update={"dev_api_token": "event-other-" + uuid4().hex})
    await bootstrap(db, other_settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(other_settings, db)),
        base_url="http://test",
        headers={"Authorization": f"Bearer {other_settings.dev_api_token}"},
    ) as other:
        for suffix in ("events", "events/stream?follow_seconds=0"):
            assert (await other.get(f"/v1/agent-runs/{run['id']}/{suffix}")).status_code == 404
    async with db.sessions.begin() as session:
        row = await session.get(AgentRun, UUID(run["id"]))
        course = await session.get(Course, UUID(row.task_input["course_id"]))
        course.teacher_id = await session.scalar(
            select(Teacher.id).where(
                Teacher.token_digest == text_digest(other_settings.dev_api_token)
            )
        )
    for suffix in ("events", "events/stream?follow_seconds=0"):
        assert (await client.get(f"/v1/agent-runs/{run['id']}/{suffix}")).status_code == 404


async def test_open_sse_closes_on_token_rotation_before_reading_later_events(
    db, settings, client, run, monkeypatch
):
    opened = asyncio.Event()
    original_read = events.read_page
    reads = 0

    async def observed_read(*args, **kwargs):
        nonlocal reads
        result = await original_read(*args, **kwargs)
        reads += 1
        if reads == 2:  # First is HTTP authorization; second is the first stream page.
            opened.set()
        return result

    monkeypatch.setattr(events, "read_page", observed_read)
    request = asyncio.create_task(
        client.get(f"/v1/agent-runs/{run['id']}/events/stream", params={"follow_seconds": 2})
    )
    await asyncio.wait_for(opened.wait(), timeout=3)
    replacement = settings.model_copy(update={"dev_api_token": "rotated-stream-" + uuid4().hex})
    await rotate_token(db, replacement)
    await append_events(db, run["id"], 1, label="not_visible_after_revocation")
    response = await asyncio.wait_for(request, timeout=3)
    assert response.status_code == 200
    frames = parse_stream(response.text)
    assert frames[-1] == {"event": "error", "data": {"code": "unauthorized"}}
    assert "not_visible_after_revocation" not in response.text
    assert [entry["data"]["sequence"] for entry in frames if entry["event"] == "agent_event"] == [1]
    assert (
        await client.get(f"/v1/agent-runs/{run['id']}/events/stream", params={"follow_seconds": 0})
    ).status_code == 401

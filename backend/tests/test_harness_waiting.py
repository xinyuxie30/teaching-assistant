"""Durable waits release leases, freeze execution time, and deduplicate structural wakeups."""

import asyncio
from datetime import timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from alembic import command
from alembic.config import Config
from conftest import post
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from teaching_assistant.api import create_app
from teaching_assistant.cli import bootstrap
from teaching_assistant.db import AgentEvent, AgentRun, AgentWait, AgentWake
from teaching_assistant.domain import digest
from teaching_assistant.harness import catalog, waiting
from teaching_assistant.harness.contracts import Reply
from teaching_assistant.harness.worker import HarnessWorker, LostAgentLease

pytestmark = pytest.mark.postgres

BRIEF = {
    "topic": "持久等待",
    "objectives": ["区分执行时间与外部等待时间"],
    "duration_minutes": 10,
}


@pytest.fixture
async def waiting_course(client):
    workspace = await post(
        client,
        "/v1/workspaces",
        {
            "course_name": "Waiting course",
            "classroom_name": "Waiting class",
            "students": [{"external_id": "waiting-1", "name": "Student"}],
        },
    )
    return workspace["course_id"]


async def start(client, course, **options):
    return await post(
        client,
        f"/v1/courses/{course}/lesson-agent-runs",
        {
            "brief": BRIEF,
            **options,
        },
    )


async def pause(client, run, **options):
    return await post(
        client,
        f"/v1/agent-runs/{run['id']}/wait",
        {
            "reason": "等待教师补充要求",
            **options,
        },
    )


async def wake(client, run, token, **options):
    return await post(
        client,
        f"/v1/agent-runs/{run['id']}/wake",
        {
            "wait_token": token,
            **options,
        },
    )


async def get_run(client, run_id):
    result = await client.get(f"/v1/agent-runs/{run_id}")
    assert result.status_code == 200, result.text
    return result.json()


def reply():
    return Reply.model_validate(
        {
            "tool_calls": [
                {
                    "id": "late-result",
                    "function": {
                        "name": "read_lesson_brief",
                        "arguments": "{}",
                    },
                }
            ],
            "metadata": {
                "requested_model": "fake",
                "returned_model": "fake",
                "adapter_version": "waiting-fixture",
                "usage": {},
            },
        }
    )


async def test_pause_fences_inflight_and_preserves_spent_budget(
    db, settings, client, waiting_course
):
    run = await start(client, waiting_course)
    worker = HarnessWorker(db, settings, run_ids=(UUID(run["id"]),))
    lease = await worker.claim()
    request, proceed = await worker.prepare_step(lease)
    assert proceed and request is not None
    saved = await pause(client, run)
    assert saved["status"] == "waiting_input"
    assert saved["models_used"] == saved["tree_models_used"] == 1
    assert saved["epoch"] == lease.epoch + 1
    assert not await HarnessWorker(db, settings).run_once()
    with pytest.raises(LostAgentLease):
        await worker.save_reply(lease, reply())
    async with db.sessions() as session:
        row = await session.get(AgentRun, UUID(run["id"]))
        assert row.inflight_seq is row.lease_until is row.lease_owner is None
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AgentEvent)
                .where(
                    AgentEvent.run_id == row.id,
                    AgentEvent.kind == "model_unknown",
                )
            )
            == 1
        )
    resumed = await wake(client, run, saved["wait"]["token"], response="请保持十分钟课时。")
    assert resumed["status"] == "queued" and resumed["models_used"] == 1
    assert await HarnessWorker(db, settings).run_once()
    finished = await get_run(client, run["id"])
    assert finished["status"] == "waiting_teacher"
    assert finished["attempts"] == 1
    async with db.sessions() as session:
        row = await session.get(AgentRun, UUID(run["id"]))
        assert any(item.get("content") == "请保持十分钟课时。" for item in row.messages)
        assert row.pending_user_messages == []


async def test_wait_freezes_deadline_and_timer_scanner_does_not_use_models(
    db,
    settings,
    client,
    waiting_course,
):
    run = await start(client, waiting_course)
    async with db.sessions() as session:
        now = await session.scalar(select(func.clock_timestamp()))
    saved = await pause(client, run, wake_at=(now + timedelta(milliseconds=500)).isoformat())
    token = UUID(saved["wait"]["token"])
    assert saved["status"] == "waiting_timer"
    assert await waiting.scan_due_waits(db) == 0
    # Simulate a stale original deadline while externally waiting; only the saved remaining
    # time is restored. There is no loop that replenishes the execution allowance.
    async with db.sessions.begin() as session:
        row = await session.get(AgentRun, UUID(run["id"]))
        row.expires_at = now - timedelta(days=1)
    await asyncio.sleep(0.52)
    assert sum(await asyncio.gather(waiting.scan_due_waits(db), waiting.scan_due_waits(db))) == 1
    assert await waiting.scan_due_waits(db) == 0
    async with db.sessions() as session:
        row = await session.get(AgentRun, UUID(run["id"]))
        snap = await session.get(AgentWait, token)
        receipt = await session.scalar(select(AgentWake).where(AgentWake.wait_id == token))
        assert receipt.source == "timer"
        assert row.models_used == row.tools_used == row.tree_models_used == row.tree_tools_used == 0
        assert row.status == "queued" and row.wait_state == {}
        remaining = snap.snapshot["nodes"][0]["remaining_seconds"]
        assert 0 < remaining <= run["limits"]["max_seconds"]
        assert abs((row.expires_at - receipt.created_at).total_seconds() - remaining) < 0.1


async def test_wake_is_idempotent_and_stale_token_does_not_resume_new_wait(
    db,
    client,
    waiting_course,
):
    run = await start(client, waiting_course)
    paused = await pause(client, run)
    token = paused["wait"]["token"]
    resumed = await wake(client, run, token, response="第一条补充")
    assert await wake(client, run, token, response="第一条补充") == resumed
    await post(
        client,
        f"/v1/agent-runs/{run['id']}/wake",
        {
            "wait_token": token,
            "response": "不同内容",
        },
        expected=409,
    )
    paused_again = await pause(client, run)
    replayed = await wake(client, run, token, response="第一条补充")
    assert replayed["status"] == "waiting_input"
    assert replayed["wait"]["token"] == paused_again["wait"]["token"]
    async with db.sessions() as session:
        row = await session.get(AgentRun, UUID(run["id"]))
        assert len(row.pending_user_messages) == 1
        assert await session.scalar(select(func.count()).select_from(AgentWake)) == 1


async def test_cancelled_timer_is_never_resurrected(db, client, waiting_course):
    run = await start(client, waiting_course)
    async with db.sessions() as session:
        now = await session.scalar(select(func.clock_timestamp()))
    paused = await pause(client, run, wake_at=(now + timedelta(milliseconds=120)).isoformat())
    cancelled = await post(client, f"/v1/agent-runs/{run['id']}/cancel", {})
    assert cancelled["status"] == "cancelled" and cancelled["wait"] is None
    await asyncio.sleep(0.13)
    assert await waiting.scan_due_waits(db) == 0
    await post(
        client,
        f"/v1/agent-runs/{run['id']}/wake",
        {
            "wait_token": paused["wait"]["token"],
        },
        expected=409,
    )
    assert await get_run(client, run["id"]) == cancelled


async def test_pause_entire_tree_preserves_paired_pending_call_and_resume_order(
    db,
    settings,
    client,
    waiting_course,
):
    run = await start(client, waiting_course, agent_profile="fake-lesson-team-agent-v1")
    assert await HarnessWorker(db, settings, run_ids=(UUID(run["id"]),)).run_once()
    async with db.sessions() as session:
        child = await session.scalar(
            select(AgentRun).where(AgentRun.parent_run_id == UUID(run["id"]))
        )
        child_id = child.id
    paused = await pause(client, run)
    assert (await get_run(client, child_id))["status"] == "waiting_input"
    assert not await HarnessWorker(db, settings, run_ids=(child_id,)).run_once()
    for action, data in (("wait", {"reason": "不可独立暂停"}), ("cancel", {})):
        await post(client, f"/v1/agent-runs/{child_id}/{action}", data, expected=409)
    resumed = await wake(client, run, paused["wait"]["token"], response="继续使用固定教学目标。")
    assert resumed["status"] == "waiting_children"
    assert await HarnessWorker(db, settings, run_ids=(child_id,)).run_once()
    assert await HarnessWorker(db, settings, run_ids=(UUID(run["id"]),)).run_once()
    assert (await get_run(client, run["id"]))["status"] == "waiting_teacher"
    async with db.sessions() as session:
        row = await session.get(AgentRun, UUID(run["id"]))
        for index, item in enumerate(row.messages):
            if item.get("content") == "继续使用固定教学目标。":
                assert row.messages[index - 1]["role"] == "tool"
                break
        else:
            pytest.fail("Teacher wake input was not applied after child result delivery")


async def test_cancelling_paused_tree_closes_all_descendants(db, settings, client, waiting_course):
    run = await start(client, waiting_course, agent_profile="fake-lesson-team-agent-v1")
    assert await HarnessWorker(db, settings).run_once()
    paused = await pause(client, run)
    await post(client, f"/v1/agent-runs/{run['id']}/cancel", {})
    async with db.sessions() as session:
        rows = list(await session.scalars(select(AgentRun)))
        assert len(rows) == 2
        assert all(row.status == "cancelled" and row.wait_state == {} for row in rows)
        assert all(row.pending_calls == [] for row in rows)
        receipt = await session.scalar(
            select(AgentWake).where(
                AgentWake.wait_id == UUID(paused["wait"]["token"]),
            )
        )
        assert receipt.source == "cancelled"


async def test_wait_authorization_validation_and_terminal_review_guard(
    db,
    settings,
    client,
    waiting_course,
):
    run = await start(client, waiting_course)
    other_settings = settings.model_copy(update={"dev_api_token": "other-" + uuid4().hex})
    await bootstrap(db, other_settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(other_settings, db)),
        base_url="http://test",
        headers={"Authorization": f"Bearer {other_settings.dev_api_token}"},
    ) as other:
        await post(other, f"/v1/agent-runs/{run['id']}/wait", {"reason": "x"}, expected=404)
        await post(
            other,
            f"/v1/agent-runs/{run['id']}/wake",
            {
                "wait_token": str(uuid4()),
            },
            expected=404,
        )
        assert (await other.get(f"/v1/agent-runs/{run['id']}/waits")).status_code == 404
    for data in (
        {"reason": " "},
        {"reason": "x", "wake_at": "2020-01-01T00:00:00"},
        {"reason": "x", "wake_at": "2020-01-01T00:00:00Z"},
    ):
        await post(client, f"/v1/agent-runs/{run['id']}/wait", data, expected=422)
    assert await HarnessWorker(db, settings).run_once()
    await post(client, f"/v1/agent-runs/{run['id']}/wait", {"reason": "x"}, expected=409)
    assert (await get_run(client, run["id"]))["status"] == "waiting_teacher"


async def test_wait_and_wake_receipts_are_immutable_and_prevent_history_loss(
    db,
    client,
    waiting_course,
):
    run = await start(client, waiting_course)
    paused = await pause(client, run)
    token = UUID(paused["wait"]["token"])
    await wake(client, run, str(token))
    for model, query in (
        (AgentWait, select(AgentWait).where(AgentWait.id == token)),
        (AgentWake, select(AgentWake).where(AgentWake.wait_id == token)),
    ):
        with pytest.raises(DBAPIError, match="immutable"):
            async with db.sessions.begin() as session:
                row = await session.scalar(query)
                if model is AgentWait:
                    row.reason = "overwritten"
                else:
                    row.response = "overwritten"
        with pytest.raises(DBAPIError, match="immutable"):
            async with db.sessions.begin() as session:
                await session.delete(await session.scalar(query))
    with pytest.raises(DBAPIError, match="durable wait history"):
        async with db.engine.begin() as connection:

            def downgrade(sync):
                config = Config("alembic.ini")
                config.attributes["connection"] = sync
                command.downgrade(config, "0008")

            await connection.run_sync(downgrade)
    history = (await client.get(f"/v1/agent-runs/{run['id']}/waits")).json()["items"]
    assert len(history) == 1 and history[0]["resolved_by"] == "teacher"


async def test_0009_round_trip_preserves_existing_root_and_checks_schema(
    db, client, waiting_course
):
    run = await start(client, waiting_course)
    async with db.engine.begin() as connection:

        def round_trip(sync):
            config = Config("alembic.ini")
            config.attributes["connection"] = sync
            command.downgrade(config, "0008")
            assert sync.scalar(text("SELECT version_num FROM alembic_version")) == "0008"
            command.upgrade(config, "head")
            command.check(config)

        await connection.run_sync(round_trip)
    assert await get_run(client, run["id"]) == run


async def test_wait_api_idempotency_and_token_scope(db, client, waiting_course):
    run = await start(client, waiting_course)
    path = f"/v1/agent-runs/{run['id']}/wait"
    saved = await post(client, path, {"reason": "等待说明"}, key="pause-once")
    assert await post(client, path, {"reason": "等待说明"}, key="pause-once") == saved
    await post(client, path, {"reason": "不同说明"}, key="pause-once", expected=409)
    other = await start(client, waiting_course)
    await post(
        client,
        f"/v1/agent-runs/{other['id']}/wake",
        {
            "wait_token": saved["wait"]["token"],
        },
        expected=404,
    )
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AgentWait)) == 1


async def test_legacy_runtime_and_expired_execution_cannot_gain_wait_capability(
    db,
    settings,
    client,
    waiting_course,
):
    run = await start(client, waiting_course)
    policy = catalog.v2_manifest(settings, "lesson-plan", "fake-lesson-agent-v1")
    async with db.sessions.begin() as session:
        row = await session.get(AgentRun, UUID(run["id"]))
        row.manifest, row.profile_id = policy, "agent-" + digest(policy)
    result = await post(
        client, f"/v1/agent-runs/{run['id']}/wait", {"reason": "旧策略"}, expected=409
    )
    assert result["code"] == "agent_wait_unsupported"
    fresh = await start(client, waiting_course)
    async with db.sessions.begin() as session:
        row = await session.get(AgentRun, UUID(fresh["id"]))
        row.expires_at = await session.scalar(select(func.clock_timestamp()))
    result = await post(
        client, f"/v1/agent-runs/{fresh['id']}/wait", {"reason": "过期"}, expected=409
    )
    assert result["code"] == "agent_deadline_exceeded"


async def test_timer_filter_can_wake_root_from_selected_child_without_touching_other_trees(
    db,
    settings,
    client,
    waiting_course,
):
    run = await start(client, waiting_course, agent_profile="fake-lesson-team-agent-v1")
    assert await HarnessWorker(db, settings).run_once()
    async with db.sessions() as session:
        child = await session.scalar(
            select(AgentRun).where(AgentRun.parent_run_id == UUID(run["id"]))
        )
        child_id = child.id
    other = await start(client, waiting_course)
    async with db.sessions() as session:
        now = await session.scalar(select(func.clock_timestamp()))
    wake_at = (now + timedelta(milliseconds=200)).isoformat()
    await pause(client, run, wake_at=wake_at)
    await pause(client, other, wake_at=wake_at)
    await asyncio.sleep(0.22)
    assert await waiting.scan_due_waits(db, run_ids=(child_id,)) == 1
    assert (await get_run(client, run["id"]))["status"] == "waiting_children"
    assert (await get_run(client, other["id"]))["status"] == "waiting_timer"
    # Claim owns the structured heartbeat and then resumes the selected root automatically.
    assert await HarnessWorker(db, settings, run_ids=(UUID(other["id"]),)).run_once()
    assert (await get_run(client, other["id"]))["status"] == "waiting_teacher"

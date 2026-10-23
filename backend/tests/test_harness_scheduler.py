"""Durable child scheduling, tree budgets, cancellation, and recovery invariants."""

import asyncio
import json
from datetime import timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from conftest import post
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from teaching_assistant.api import create_app
from teaching_assistant.cli import bootstrap
from teaching_assistant.db import AgentArtifact, AgentEvent, AgentRun
from teaching_assistant.domain import DomainError, ModelError
from teaching_assistant.harness import scheduler
from teaching_assistant.harness.contracts import Reply
from teaching_assistant.harness.team_lesson import TeamLessonTask
from teaching_assistant.harness.worker import HarnessWorker, LostAgentLease

pytestmark = pytest.mark.postgres

BRIEF = {
    "topic": "可复核的版本记录",
    "objectives": ["用例子说明记录不可覆盖的原因"],
    "duration_minutes": 10,
}


@pytest.fixture
async def scheduler_course(client):
    workspace = await post(
        client,
        "/v1/workspaces",
        {
            "course_name": "Durable scheduling course",
            "classroom_name": "Scheduling class",
            "students": [{"external_id": "schedule-1", "name": "Student"}],
        },
    )
    return workspace["course_id"]


async def start(client, course, **options):
    return await post(
        client,
        f"/v1/courses/{course}/lesson-agent-runs",
        {"brief": BRIEF, "agent_profile": "fake-lesson-team-agent-v1", **options},
    )


async def detail(client, run_id):
    response = await client.get(f"/v1/agent-runs/{run_id}")
    assert response.status_code == 200, response.text
    return response.json()


async def children(db, run_id):
    async with db.sessions() as session:
        return list(
            await session.scalars(
                select(AgentRun)
                .where(AgentRun.parent_run_id == UUID(str(run_id)))
                .order_by(AgentRun.created_at, AgentRun.id)
            )
        )


async def waiting_parent(db, settings, client, course, **options):
    root = await start(client, course, **options)
    worker = HarnessWorker(db, settings, run_ids=(UUID(root["id"]),))
    assert await worker.run_once()
    assert (await detail(client, root["id"]))["status"] == "waiting_children"
    spawned = await children(db, root["id"])
    assert len(spawned) == 1
    return root, spawned[0]


async def test_resumed_parent_gets_full_lease_after_child_receipt(
    db, settings, client, scheduler_course
):
    root, child = await waiting_parent(db, settings, client, scheduler_course)
    assert await HarnessWorker(db, settings, run_ids=(child.id,)).run_once()
    worker = HarnessWorker(db, settings, run_ids=(UUID(root["id"]),))
    lease = await worker.claim()
    assert lease is not None and lease.run_id == UUID(root["id"])
    async with db.sessions() as session:
        run = await session.get(AgentRun, lease.run_id)
        # ready_at records receipt completion; that work must not spend the new lease.
        assert run.lease_until >= run.ready_at + timedelta(seconds=settings.lease_seconds)
        assert run.attempts == 1 and not run.pending_calls
    await worker.execute(lease)
    assert (await detail(client, root["id"]))["status"] == "waiting_teacher"


async def drain(db, settings, *, limit=12):
    worker = HarnessWorker(db, settings)
    for _ in range(limit):
        if not await worker.run_once():
            return
    pytest.fail("The bounded scheduling fixture did not become idle")


def tool_reply(name, arguments, call_id="fixture-call"):
    return Reply.model_validate(
        {
            "tool_calls": [
                {
                    "id": call_id,
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ],
            "metadata": {
                "requested_model": "fixture",
                "returned_model": "fixture",
                "adapter_version": "scheduler-fixture",
                "usage": {},
            },
        }
    )


async def test_child_is_isolated_and_parent_awaits_its_durable_result(
    db, settings, client, scheduler_course
):
    root, child = await waiting_parent(
        db,
        settings,
        client,
        scheduler_course,
        instructions="PARENT-ONLY-CONTEXT: 按任务工具生成教学方案。",
    )
    assert child.task_type == "lesson-practice"
    assert child.root_run_id == child.parent_run_id == UUID(root["id"])
    assert child.depth == 1
    assert child.task_input["course_id"] == scheduler_course
    assert child.task_input["brief"] == BRIEF
    assert "PARENT-ONLY-CONTEXT" not in json.dumps(child.messages, ensure_ascii=False)
    async with db.sessions() as session:
        parent = await session.get(AgentRun, UUID(root["id"]))
        assert child.teacher_id == parent.teacher_id
        assert child.expires_at <= parent.expires_at
        assert parent.lease_owner is parent.lease_until is None
        assert any(call["id"] == child.parent_tool_call_id for call in parent.pending_calls)
        assert not any(
            message.get("tool_call_id") == child.parent_tool_call_id for message in parent.messages
        )

    assert await HarnessWorker(db, settings, run_ids=(child.id,)).run_once()
    assert (await detail(client, child.id))["status"] == "completed"
    child_artifact = (await client.get(f"/v1/agent-runs/{child.id}/artifact")).json()
    assert child_artifact["confirmed_at"] is None
    assert await HarnessWorker(db, settings, run_ids=(UUID(root["id"]),)).run_once()
    saved = await detail(client, root["id"])
    assert saved["status"] == "waiting_teacher"
    assert saved["tree_models_used"] == saved["tree_tools_used"] == 5
    listed = await client.get(f"/v1/agent-runs/{root['id']}/children")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["items"]] == [str(child.id)]
    parent_artifact = (await client.get(f"/v1/agent-runs/{root['id']}/artifact")).json()
    assert parent_artifact["body"]["practice"] == child_artifact["body"]["practice"]
    assert parent_artifact["confirmed_at"] is None
    async with db.sessions() as session:
        parent = await session.get(AgentRun, UUID(root["id"]))
        calls = [call["id"] for item in parent.messages for call in item.get("tool_calls", [])]
        results = [item["tool_call_id"] for item in parent.messages if item["role"] == "tool"]
        assert calls == results and len(calls) == len(set(calls))
        delegated = [
            json.loads(item["content"])
            for item in parent.messages
            if item.get("tool_call_id") == child.parent_tool_call_id
        ]
        assert len(delegated) == 1 and delegated[0]["ok"]
        assert delegated[0]["result"]["child_run_id"] == str(child.id)
        assert delegated[0]["result"]["confirmed"] is False


async def test_concurrent_child_claim_and_parent_restart_do_not_repeat_spawn_or_delivery(
    db, settings, client, scheduler_course
):
    root, child = await waiting_parent(db, settings, client, scheduler_course)
    workers = [HarnessWorker(db, settings, run_ids=(child.id,)) for _ in range(2)]
    claims = await asyncio.gather(*(worker.run_once() for worker in workers))
    assert sum(claims) == 1
    await drain(db, settings)
    saved = await detail(client, root["id"])
    assert saved["status"] == "waiting_teacher"
    assert saved["attempts"] == 1
    assert len(await children(db, root["id"])) == 1
    assert not await HarnessWorker(db, settings).run_once()
    assert await detail(client, root["id"]) == saved
    async with db.sessions() as session:
        parent = await session.get(AgentRun, UUID(root["id"]))
        child_row = await session.get(AgentRun, child.id)
        assert child_row.models_used == child_row.tools_used == 2
        assert parent.tools_used == 3
        assert (
            sum(item.get("tool_call_id") == child.parent_tool_call_id for item in parent.messages)
            == 1
        )
        assert await session.scalar(select(func.count()).select_from(AgentArtifact)) == 2


async def test_cancel_waiting_parent_fences_child_inflight_and_closes_the_tree(
    db, settings, client, scheduler_course
):
    root, child = await waiting_parent(db, settings, client, scheduler_course)
    worker = HarnessWorker(db, settings, run_ids=(child.id,))
    lease = await worker.claim()
    request, proceed = await worker.prepare_step(lease)
    assert proceed and request is not None
    cancelled = await post(client, f"/v1/agent-runs/{root['id']}/cancel", {})
    assert cancelled["status"] == "cancelled"
    assert (await detail(client, child.id))["status"] == "cancelled"
    with pytest.raises(LostAgentLease):
        await worker.save_reply(lease, tool_reply("read_practice_brief", {}))
    await worker.execute(lease)
    assert not await HarnessWorker(db, settings).run_once()
    async with db.sessions() as session:
        rows = list(await session.scalars(select(AgentRun)))
        assert len(rows) == 2
        for row in rows:
            assert row.status == "cancelled"
            assert row.lease_owner is row.lease_until is row.inflight_seq is None
            assert row.pending_calls == []
            calls = [call["id"] for item in row.messages for call in item.get("tool_calls", [])]
            results = [item["tool_call_id"] for item in row.messages if item["role"] == "tool"]
            assert calls == results
        assert await session.scalar(select(func.count()).select_from(AgentArtifact)) == 0
        unknown = await session.scalar(
            select(func.count())
            .select_from(AgentEvent)
            .where(AgentEvent.run_id == child.id, AgentEvent.kind == "model_unknown")
        )
        assert unknown == 1


@pytest.mark.parametrize("resource,limit", [("model", 4), ("tool", 4)])
async def test_root_budget_caps_all_child_work(
    db, settings, client, scheduler_course, resource, limit
):
    options = {"max_model_calls": 12, "max_tool_calls": 24, f"max_{resource}_calls": limit}
    root = await start(client, scheduler_course, **options)
    await drain(db, settings)
    saved = await detail(client, root["id"])
    assert saved["status"] == "failed"
    assert "budget" in saved["error_code"]
    async with db.sessions() as session:
        rows = list(await session.scalars(select(AgentRun)))
        assert len(rows) == 2
        used = sum(getattr(row, f"{resource}s_used") for row in rows)
        assert used == limit
        assert all(row.status not in {"queued", "running", "waiting_children"} for row in rows)
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AgentArtifact)
                .where(AgentArtifact.run_id == UUID(root["id"]))
            )
            == 0
        )


async def test_child_failure_becomes_one_tool_error_before_parent_resumes(
    db, settings, client, scheduler_course
):
    root, child = await waiting_parent(db, settings, client, scheduler_course)
    worker = HarnessWorker(db, settings, run_ids=(child.id,))
    lease = await worker.claim()
    request, proceed = await worker.prepare_step(lease)
    assert proceed and request is not None
    await worker.fail(lease, ModelError("fixture_child_failure", retryable=False))
    assert (await detail(client, child.id))["status"] == "failed"
    parent_worker = HarnessWorker(db, settings, run_ids=(UUID(root["id"]),))
    parent_lease = await parent_worker.claim()
    assert parent_lease is not None
    async with db.sessions() as session:
        parent = await session.get(AgentRun, UUID(root["id"]))
        outputs = [
            json.loads(item["content"])
            for item in parent.messages
            if item.get("tool_call_id") == child.parent_tool_call_id
        ]
        assert len(outputs) == 1 and outputs[0]["ok"] is False
        assert "error_code" in outputs[0]
        assert parent.pending_calls == []
    await parent_worker.execute(parent_lease)
    await drain(db, settings)
    assert (await detail(client, root["id"]))["status"] not in {
        "queued",
        "running",
        "waiting_children",
    }


async def test_waiting_parent_deadline_reaps_queued_child(db, settings, client, scheduler_course):
    root, child = await waiting_parent(db, settings, client, scheduler_course)
    async with db.sessions.begin() as session:
        parent = await session.get(AgentRun, UUID(root["id"]))
        parent.expires_at = (await session.scalar(select(func.clock_timestamp()))) - timedelta(
            seconds=1
        )
    await drain(db, settings)
    saved = await detail(client, root["id"])
    assert saved["status"] == "failed" and saved["error_code"] == "agent_deadline_exceeded"
    after = await detail(client, child.id)
    assert after["status"] in {"failed", "cancelled"}
    assert after["models_used"] == after["tools_used"] == 0


async def test_completed_child_cannot_resurrect_cancelled_parent(
    db, settings, client, scheduler_course
):
    root, child = await waiting_parent(db, settings, client, scheduler_course)
    assert await HarnessWorker(db, settings, run_ids=(child.id,)).run_once()
    assert (await detail(client, child.id))["status"] == "completed"
    cancelled = await post(client, f"/v1/agent-runs/{root['id']}/cancel", {})
    assert cancelled["status"] == "cancelled"
    assert not await HarnessWorker(db, settings).run_once()
    assert await detail(client, root["id"]) == cancelled
    assert (await detail(client, child.id))["status"] == "completed"
    async with db.sessions() as session:
        parent = await session.get(AgentRun, UUID(root["id"]))
        assert parent.pending_calls == []
        assert (
            sum(item.get("tool_call_id") == child.parent_tool_call_id for item in parent.messages)
            == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AgentArtifact)
                .where(AgentArtifact.run_id == parent.id)
            )
            == 0
        )


async def test_spawn_savepoint_rollback_preserves_budget_receipt_without_orphan(
    db, settings, client, scheduler_course, monkeypatch
):
    root = await start(client, scheduler_course)
    worker = HarnessWorker(db, settings, run_ids=(UUID(root["id"]),))
    lease = await worker.claim()
    request, _ = await worker.prepare_step(lease)
    assert await worker.save_reply(lease, await worker.invoke(lease, *request))
    assert await worker.prepare_step(lease) == (None, True)  # Read the parent brief.
    request, _ = await worker.prepare_step(lease)
    assert await worker.save_reply(lease, await worker.invoke(lease, *request))

    original_spawn = scheduler.spawn_child

    async def interrupted_spawn(*args, **kwargs):
        await original_spawn(*args, **kwargs)
        raise DomainError("fixture_spawn_failure", "Fail after saving the new child")

    monkeypatch.setattr(scheduler, "spawn_child", interrupted_spawn)
    assert await worker.prepare_step(lease) == (None, True)
    assert await children(db, root["id"]) == []
    async with db.sessions() as session:
        parent = await session.get(AgentRun, UUID(root["id"]))
        assert parent.status == "running" and parent.children_spawned == 0
        assert parent.tools_used == parent.tree_tools_used == 2
        assert parent.pending_calls == []
        result = json.loads(parent.messages[-1]["content"])
        assert result["ok"] is False and result["error_code"] == "fixture_spawn_failure"


async def test_child_ancestry_cannot_be_rewritten_after_creation(
    db, settings, client, scheduler_course
):
    root, child = await waiting_parent(db, settings, client, scheduler_course)
    with pytest.raises(DBAPIError, match="ancestry is immutable"):
        async with db.sessions.begin() as session:
            row = await session.get(AgentRun, child.id)
            row.parent_tool_call_id = "rewritten-parent-call"
    async with db.sessions() as session:
        row = await session.get(AgentRun, child.id)
        assert row.parent_run_id == row.root_run_id == UUID(root["id"])
        assert row.parent_tool_call_id == child.parent_tool_call_id


async def test_failed_parent_handoff_rolls_back_state_and_can_retry_without_losing_child(
    db, settings, client, scheduler_course, monkeypatch
):
    root, child = await waiting_parent(
        db, settings, client, scheduler_course, max_model_calls=12, max_tool_calls=24
    )
    assert await HarnessWorker(db, settings, run_ids=(child.id,)).run_once()
    artifact = (await client.get(f"/v1/agent-runs/{child.id}/artifact")).json()
    original = TeamLessonTask.apply_result

    async def interrupted_handoff(self, session, run, name, result):
        await original(self, session, run, name, result)
        if name == "request_practice_agent":
            await session.flush()
            raise DomainError("fixture_handoff_failure", "Fail after applying the child result")

    monkeypatch.setattr(TeamLessonTask, "apply_result", interrupted_handoff)
    parent_worker = HarnessWorker(db, settings, run_ids=(UUID(root["id"]),))
    lease = await parent_worker.claim()
    assert lease is not None
    async with db.sessions() as session:
        parent = await session.get(AgentRun, UUID(root["id"]))
        assert parent.status == "running" and parent.attempts == 1
        assert parent.task_state == {"brief_read": True}
        assert parent.pending_calls == []
        outputs = [
            json.loads(item["content"])
            for item in parent.messages
            if item.get("tool_call_id") == child.parent_tool_call_id
        ]
        assert outputs == [{"ok": False, "error_code": "fixture_handoff_failure"}]
        assert (await session.get(AgentRun, child.id)).status == "completed"
    assert (await client.get(f"/v1/agent-runs/{child.id}/artifact")).json() == artifact

    monkeypatch.setattr(TeamLessonTask, "apply_result", original)
    await parent_worker.execute(lease)
    await drain(db, settings)
    assert (await detail(client, root["id"]))["status"] == "waiting_teacher"
    assert len(await children(db, root["id"])) == 2
    assert (await client.get(f"/v1/agent-runs/{child.id}/artifact")).json() == artifact
    async with db.sessions() as session:
        parent = await session.get(AgentRun, UUID(root["id"]))
        assert (
            sum(item.get("tool_call_id") == child.parent_tool_call_id for item in parent.messages)
            == 1
        )


async def test_child_cannot_expand_its_tools_or_create_grandchildren(
    db, settings, client, scheduler_course, monkeypatch
):
    # Leave enough root budget to observe the child's own bounded rejection.
    root, child = await waiting_parent(db, settings, client, scheduler_course, max_model_calls=12)

    class EscalatingChild:
        async def respond(self, body, policy):
            return tool_reply(
                "request_practice_agent",
                {"instructions": "创建另一个子任务"},
                f"nested-{len(body['messages'])}",
            )

    monkeypatch.setattr(HarnessWorker, "build_model", lambda *_: EscalatingChild())
    assert await HarnessWorker(db, settings, run_ids=(child.id,)).run_once()
    assert (await detail(client, child.id))["status"] == "failed"
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AgentRun)) == 2
        events = list(
            await session.scalars(
                select(AgentEvent).where(
                    AgentEvent.run_id == child.id, AgentEvent.kind == "post_tool"
                )
            )
        )
        assert events and all(
            event.body["output"]["error_code"] == "tool_not_allowed" for event in events
        )
    assert len(await children(db, root["id"])) == 1


async def test_child_routes_preserve_teacher_scope_and_cannot_confirm_internal_work(
    db, settings, client, scheduler_course
):
    root, child = await waiting_parent(db, settings, client, scheduler_course)
    await post(
        client,
        f"/v1/courses/{scheduler_course}/lesson-agent-runs",
        {"brief": BRIEF, "agent_profile": "fake-practice-agent-v1"},
        expected=422,
    )
    await HarnessWorker(db, settings, run_ids=(child.id,)).run_once()
    artifact = (await client.get(f"/v1/agent-runs/{child.id}/artifact")).json()
    await post(
        client,
        f"/v1/agent-runs/{child.id}/confirm",
        {"content_hash": artifact["content_hash"]},
        expected=409,
    )
    other_settings = settings.model_copy(update={"dev_api_token": "other-" + uuid4().hex})
    await bootstrap(db, other_settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(other_settings, db)),
        base_url="http://test",
        headers={"Authorization": f"Bearer {other_settings.dev_api_token}"},
    ) as other:
        for run_id in (root["id"], str(child.id)):
            for suffix in ("", "/events", "/artifact", "/children"):
                assert (await other.get(f"/v1/agent-runs/{run_id}{suffix}")).status_code == 404
            await post(other, f"/v1/agent-runs/{run_id}/cancel", {}, expected=404)


@pytest.mark.parametrize("scope_override", [False, True])
async def test_delegation_preserves_read_preconditions_and_denies_model_scope_override(
    db, settings, client, scheduler_course, monkeypatch, scope_override
):
    arguments = {"instructions": "起草一个练习"}
    if scope_override:
        arguments["teacher_id"] = str(uuid4())

    class PrematureDelegation:
        async def respond(self, body, policy):
            return tool_reply("request_practice_agent", arguments)

    monkeypatch.setattr(HarnessWorker, "build_model", lambda *_: PrematureDelegation())
    root = await start(client, scheduler_course, max_tool_calls=1)
    assert await HarnessWorker(db, settings, run_ids=(UUID(root["id"]),)).run_once()
    assert (await detail(client, root["id"]))["status"] == "failed"
    assert await children(db, root["id"]) == []
    async with db.sessions() as session:
        event = await session.scalar(
            select(AgentEvent).where(
                AgentEvent.run_id == UUID(root["id"]), AgentEvent.kind == "post_tool"
            )
        )
        assert event.body["output"]["error_code"] == (
            "invalid_tool_arguments" if scope_override else "read_lesson_brief_required"
        )

"""Teacher wake feedback invalidates effects proposed before a durable pause."""

import json
from uuid import UUID

import pytest
from conftest import post
from sqlalchemy import func, select
from teaching_assistant.db import AgentArtifact, AgentEvent, AgentRun
from teaching_assistant.harness.contracts import Reply
from teaching_assistant.harness.worker import HarnessWorker

pytestmark = pytest.mark.postgres
FEEDBACK = "请将活动标题标记为教师补充要求已应用，保留原有教学目标。"


async def start(client, *, team=False):
    workspace = await post(
        client,
        "/v1/workspaces",
        {
            "course_name": "Wake feedback course",
            "classroom_name": "Wake feedback class",
            "students": [{"external_id": "feedback-1", "name": "Student"}],
        },
    )
    return await post(
        client,
        f"/v1/courses/{workspace['course_id']}/lesson-agent-runs",
        {
            "agent_profile": "fake-lesson-team-agent-v1" if team else "fake-lesson-agent-v1",
            "brief": {
                "topic": "反馈恢复",
                "objectives": ["恢复后先处理教师的新要求"],
                "duration_minutes": 10,
            },
        },
    )


async def pause_with_proposal(db, settings, client, *, team=False, leading_read=False):
    run = await start(client, team=team)
    run_id = UUID(run["id"])
    worker = HarnessWorker(db, settings, run_ids=(run_id,))
    lease = await worker.claim()
    read, proceed = await worker.prepare_step(lease)
    assert proceed
    await worker.save_reply(lease, await worker.invoke(lease, *read))
    assert await worker.prepare_step(lease) == (None, True)
    proposed, proceed = await worker.prepare_step(lease)
    assert proceed
    reply = await worker.invoke(lease, *proposed)
    old_call = reply.tool_calls[0]
    assert old_call.function.name == ("request_practice_agent" if team else "propose_lesson_plan")
    if leading_read:
        data = reply.model_dump(mode="json")
        data["tool_calls"].insert(
            0,
            {
                "id": "before-pause-read",
                "function": {"name": "read_lesson_brief", "arguments": "{}"},
            },
        )
        reply = Reply.model_validate(data)
    await worker.save_reply(lease, reply)
    paused = await post(client, f"/v1/agent-runs/{run_id}/wait", {"reason": "在副作用前补充要求"})
    await post(
        client,
        f"/v1/agent-runs/{run_id}/wake",
        {"wait_token": paused["wait"]["token"], "response": FEEDBACK},
    )
    resumed = HarnessWorker(db, settings, run_ids=(run_id,))
    resumed_lease = await resumed.claim()
    if leading_read:
        assert await resumed.prepare_step(resumed_lease) == (None, True)
    request, proceed = await resumed.prepare_step(resumed_lease)
    assert proceed and request is not None
    # The stale call receives its paired error before the new user message.
    messages = request[0]["messages"]
    feedback_index = next(
        i for i, message in enumerate(messages) if message.get("content") == FEEDBACK
    )
    receipt = messages[feedback_index - 1]
    assert receipt["role"] == "tool" and receipt["tool_call_id"] == old_call.id
    assert json.loads(receipt["content"]) == {"ok": False, "error_code": "teacher_input_changed"}
    async with db.sessions() as session:
        row = await session.get(AgentRun, run_id)
        assert not row.pending_user_messages
        assert await session.scalar(select(func.count()).select_from(AgentArtifact)) == 0
        assert (
            await session.scalar(
                select(func.count()).select_from(AgentRun).where(AgentRun.parent_run_id == run_id)
            )
            == 0
        )
        skipped = await session.scalar(
            select(AgentEvent).where(AgentEvent.run_id == run_id, AgentEvent.kind == "tool_skipped")
        )
        assert skipped.body["tool_call_id"] == old_call.id
    return run_id, resumed, resumed_lease, request, old_call


@pytest.mark.parametrize("leading_read", [False, True])
async def test_wake_feedback_prevents_stale_final_write_before_new_model_turn(
    db, settings, client, leading_read
):
    run_id, worker, lease, request, old_call = await pause_with_proposal(
        db, settings, client, leading_read=leading_read
    )
    updated = await worker.invoke(lease, *request)
    assert updated.tool_calls[0].id != old_call.id
    data = updated.model_dump(mode="json")
    arguments = json.loads(data["tool_calls"][0]["function"]["arguments"])
    arguments["title"] = "教师补充要求已应用"
    data["tool_calls"][0]["function"]["arguments"] = json.dumps(arguments, ensure_ascii=False)
    await worker.save_reply(lease, Reply.model_validate(data))
    await worker.execute(lease)
    async with db.sessions() as session:
        row = await session.get(AgentRun, run_id)
        artifact = await session.scalar(select(AgentArtifact).where(AgentArtifact.run_id == run_id))
        assert row.status == "waiting_teacher" and not row.pending_user_messages
        assert artifact.body["title"] == "教师补充要求已应用"
        assert row.models_used == 3
        assert row.tools_used == (3 if leading_read else 2)
        assert not await session.scalar(
            select(AgentEvent.id).where(
                AgentEvent.run_id == run_id,
                AgentEvent.kind == "pre_tool",
                AgentEvent.body["tool_call_id"].astext == old_call.id,
            )
        )


async def test_wake_feedback_prevents_undispatched_stale_delegation(db, settings, client):
    run_id, worker, lease, request, old_call = await pause_with_proposal(
        db, settings, client, team=True
    )
    updated = await worker.invoke(lease, *request)
    assert updated.tool_calls[0].function.name == "request_practice_agent"
    assert updated.tool_calls[0].id != old_call.id
    await worker.save_reply(lease, updated)
    await worker.execute(lease)
    async with db.sessions() as session:
        row = await session.get(AgentRun, run_id)
        children = list(
            await session.scalars(select(AgentRun).where(AgentRun.parent_run_id == run_id))
        )
        assert row.status == "waiting_children" and len(children) == 1
        assert children[0].parent_tool_call_id == updated.tool_calls[0].id
        child_id = children[0].id
    assert await HarnessWorker(db, settings, run_ids=(child_id,)).run_once()
    assert await HarnessWorker(db, settings, run_ids=(run_id,)).run_once()
    async with db.sessions() as session:
        row = await session.get(AgentRun, run_id)
        assert row.status == "waiting_teacher" and not row.pending_user_messages
        assert row.tree_models_used == 6

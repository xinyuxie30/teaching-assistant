"""New manual feedback invalidates an undispatched question from the old reply."""

import json
from uuid import UUID

import pytest
from conftest import post
from sqlalchemy import select
from teaching_assistant.db import AgentRun, AgentWait
from teaching_assistant.domain import digest
from teaching_assistant.harness.contracts import Reply
from teaching_assistant.harness.lesson import FakeLessonModel
from teaching_assistant.harness.worker import HarnessWorker

pytestmark = pytest.mark.postgres


async def test_manual_wake_feedback_cancels_old_question_before_it_creates_another_wait(
    db, settings, client, monkeypatch
):
    course = await post(
        client,
        "/v1/workspaces",
        {
            "course_name": "Question feedback course",
            "classroom_name": "Synthetic class",
            "students": [{"external_id": "f1", "name": "Synthetic student"}],
        },
    )
    run = await post(
        client,
        f"/v1/courses/{course['course_id']}/lesson-agent-runs",
        {
            "agent_profile": "fake-assisted-lesson-agent-v1",
            "brief": {
                "topic": "明确教学要求",
                "objectives": ["说明版本的作用"],
                "duration_minutes": 10,
            },
        },
    )
    run_id = UUID(run["id"])
    worker = HarnessWorker(db, settings, run_ids=(run_id,))
    lease = await worker.claim()
    old_question = None
    # Persist the third model reply, then interrupt before its question tool executes.
    for _ in range(6):
        prepared, ongoing = await worker.prepare_step(lease)
        assert ongoing
        if prepared:
            reply = await worker.invoke(lease, *prepared)
            assert await worker.save_reply(lease, reply)
            if reply.tool_calls[0].function.name == "ask_teacher":
                old_question = reply.tool_calls[0].id
                break
    assert old_question is not None
    paused = await post(client, f"/v1/agent-runs/{run_id}/wait", {"reason": "教师补充完整要求"})
    feedback = "信息已经给齐，不要再提问；请采用先独立思考再同伴讨论的安排。"
    await post(
        client,
        f"/v1/agent-runs/{run_id}/wake",
        {"wait_token": paused["wait"]["token"], "response": feedback},
    )
    seen = []

    class FeedbackModel:
        async def respond(self, body, policy):
            seen.append(body)
            assert body["messages"][-1] == {"role": "user", "content": feedback}
            base = await FakeLessonModel().respond(body, policy)
            arguments = json.loads(base.tool_calls[0].function.arguments)
            arguments.update(memory_references=[], teacher_response=None)
            return Reply.model_validate(
                {
                    "tool_calls": [
                        {
                            "id": "feedback-" + digest(body)[:32],
                            "function": {
                                "name": "propose_assisted_lesson_plan",
                                "arguments": json.dumps(arguments, ensure_ascii=False),
                            },
                        }
                    ],
                    "metadata": base.metadata.model_dump(mode="json"),
                }
            )

    monkeypatch.setattr("teaching_assistant.harness.worker.build_model", lambda *_: FeedbackModel())
    assert await HarnessWorker(db, settings, run_ids=(run_id,)).run_once()
    assert len(seen) == 1
    async with db.sessions() as session:
        row = await session.get(AgentRun, run_id)
        assert row.status == "waiting_teacher"
        assert row.models_used == row.tree_models_used == 4
        assert row.tools_used == row.tree_tools_used == 3
        assert not row.pending_calls and not row.pending_user_messages
        receipts = [msg for msg in row.messages if msg.get("tool_call_id") == old_question]
        assert len(receipts) == 1
        assert json.loads(receipts[0]["content"])["error_code"] == "teacher_input_changed"
        waits = list(await session.scalars(select(AgentWait).where(AgentWait.run_id == run_id)))
        assert len(waits) == 1 and "question" not in waits[0].snapshot

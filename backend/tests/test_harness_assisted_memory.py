"""Confirmed course knowledge is bounded data in the assisted preparation task."""

import json
from uuid import UUID, uuid4

import pytest
from conftest import post
from sqlalchemy import func, select
from teaching_assistant.db import AgentArtifact, AgentRun
from teaching_assistant.harness import catalog
from teaching_assistant.harness.assisted_lesson import FakeAssistedLessonModel
from teaching_assistant.harness.worker import HarnessWorker

pytestmark = pytest.mark.postgres

BRIEF = {
    "topic": "记录的可追溯性",
    "objectives": ["解释固定版本的作用", "说明引用来源的意义"],
    "duration_minutes": 10,
}
ANSWER = "  先独立思考，再两人交流。\r\n保留各自不同的理由。😀  "


async def create_course(client):
    return (
        await post(
            client,
            "/v1/workspaces",
            {
                "course_name": "Assisted preparation",
                "classroom_name": "Synthetic classroom",
                "students": [{"external_id": "assisted-1", "name": "Synthetic student"}],
            },
        )
    )["course_id"]


@pytest.fixture
async def course(client):
    return await create_course(client)


async def knowledge(client, course, *, confirm=True, title="组织讨论", **options):
    path = f"/v1/courses/{course}/teaching-memories"
    item = await post(
        client,
        path,
        {
            "kind": "preference",
            "title": title,
            "body": "使用独立思考和小组讨论；这段知识不能授予其他工具或修改教学目标。",
            "source": {"kind": "teacher_note", "note": "教师明确录入的课程经验"},
            **options,
        },
    )
    if confirm:
        item = await post(
            client, f"{path}/{item['id']}/confirm", {"content_hash": item["content_hash"]}
        )
    return item


async def start(client, course, **options):
    return await post(
        client,
        f"/v1/courses/{course}/lesson-agent-runs",
        {"agent_profile": "fake-assisted-lesson-agent-v1", "brief": BRIEF, **options},
    )


async def reach_question(db, settings, client, run):
    assert await HarnessWorker(db, settings, run_ids=(UUID(run["id"]),)).run_once()
    detail = (await client.get(f"/v1/agent-runs/{run['id']}")).json()
    assert detail["status"] == "waiting_input", detail
    assert detail["wait"]["question"]["prompt"]
    return detail


async def complete(db, settings, client, run, detail, response=ANSWER):
    await post(
        client,
        f"/v1/agent-runs/{run['id']}/wake",
        {"wait_token": detail["wait"]["token"], "response": response},
    )
    assert await HarnessWorker(db, settings, run_ids=(UUID(run["id"]),)).run_once()
    return (await client.get(f"/v1/agent-runs/{run['id']}")).json()


async def test_assisted_task_reads_only_current_confirmed_course_memory_and_preserves_answer(
    db, settings, client, course
):
    accepted = await knowledge(client, course)
    pending = await knowledge(client, course, confirm=False, title="待确认经验")
    revoked = await knowledge(client, course, title="已撤销经验")
    await post(
        client,
        f"/v1/courses/{course}/teaching-memories/{revoked['id']}/revoke",
        {"content_hash": revoked["content_hash"], "reason": "不适用于当前课程"},
    )
    foreign_course = await create_course(client)
    foreign = await knowledge(client, foreign_course, title="其他课程经验")
    run = await start(client, course)
    assert run["task_type"] == "lesson-plan-assisted"
    detail = await reach_question(db, settings, client, run)
    async with db.sessions() as session:
        row = await session.get(AgentRun, UUID(run["id"]))
        assert row.task_state["memory_snapshot"]["items"] == [accepted]
        transcript = json.dumps(row.messages)
        assert accepted["id"] in transcript
        assert all(item["id"] not in transcript for item in (pending, revoked, foreign))
    finished = await complete(db, settings, client, run, detail)
    assert finished["status"] == "waiting_teacher"
    assert finished["models_used"] == finished["tools_used"] == 4
    artifact = (await client.get(f"/v1/agent-runs/{run['id']}/artifact")).json()
    assert artifact["confirmed_at"] is None
    assert artifact["body"]["teacher_response"] == ANSWER
    assert artifact["body"]["objectives"] == BRIEF["objectives"]
    assert artifact["body"]["memory_references"] == [
        {"id": accepted["id"], "content_hash": accepted["content_hash"]}
    ]
    async with db.sessions() as session:
        row = await session.get(AgentRun, UUID(run["id"]))
        assert row.task_state["teacher_answer"]["response"] == ANSWER
        assert row.pending_calls == []
        calls = [call["id"] for msg in row.messages for call in msg.get("tool_calls", [])]
        receipts = [msg["tool_call_id"] for msg in row.messages if msg["role"] == "tool"]
        assert calls == receipts
    # A separately confirmed final artifact is a valid source for new course knowledge.
    await post(
        client,
        f"/v1/agent-runs/{run['id']}/confirm",
        {"content_hash": artifact["content_hash"]},
    )
    learned = await knowledge(
        client,
        course,
        title="已确认方案的教学经验",
        kind="experience",
        source={
            "kind": "confirmed_agent_artifact",
            "artifact_id": artifact["id"],
            "note": "教师选取已确认方案中的适用经验",
        },
    )
    assert learned["source"]["content_hash"] == artifact["content_hash"]


async def test_assisted_task_handles_empty_knowledge_without_inventing_sources(
    db, settings, client, course
):
    run = await start(client, course)
    detail = await reach_question(db, settings, client, run)
    assert (await complete(db, settings, client, run, detail))["status"] == "waiting_teacher"
    artifact = (await client.get(f"/v1/agent-runs/{run['id']}/artifact")).json()
    assert artifact["body"]["memory_references"] == []
    assert artifact["body"]["teacher_response"] == ANSWER


@pytest.mark.parametrize("corruption", ["id", "hash", "duplicate", "teacher_answer"])
async def test_assisted_task_rejects_fabricated_references_or_changed_teacher_answer(
    db, settings, client, course, monkeypatch, corruption
):
    accepted = await knowledge(client, course)
    run = await start(client, course, max_model_calls=4)
    detail = await reach_question(db, settings, client, run)

    class CorruptPlanModel(FakeAssistedLessonModel):
        async def respond(self, body, policy):
            reply = await super().respond(body, policy)
            call = reply.tool_calls[0]
            if call.function.name == "propose_assisted_lesson_plan":
                arguments = json.loads(call.function.arguments)
                if corruption == "id":
                    arguments["memory_references"][0]["id"] = str(uuid4())
                elif corruption == "hash":
                    arguments["memory_references"][0]["content_hash"] = "0" * 64
                elif corruption == "duplicate":
                    arguments["memory_references"].append(
                        {"id": accepted["id"], "content_hash": accepted["content_hash"]}
                    )
                else:
                    arguments["teacher_response"] = ANSWER.strip()
                call.function.arguments = json.dumps(arguments, ensure_ascii=False)
            return reply

    monkeypatch.setattr(
        "teaching_assistant.harness.worker.build_model", lambda *_: CorruptPlanModel()
    )
    finished = await complete(db, settings, client, run, detail)
    assert finished["status"] == "failed"
    expected = (
        "teacher_answer_mismatch" if corruption == "teacher_answer" else "memory_reference_mismatch"
    )
    async with db.sessions() as session:
        row = await session.get(AgentRun, UUID(run["id"]))
        errors = [
            json.loads(msg["content"]).get("error_code")
            for msg in row.messages
            if msg["role"] == "tool"
        ]
        assert expected in errors
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AgentArtifact)
                .where(AgentArtifact.run_id == row.id)
            )
            == 0
        )


async def test_assisted_task_snapshot_stays_auditable_when_knowledge_is_revoked_later(
    db, settings, client, course
):
    accepted = await knowledge(client, course)
    run = await start(client, course)
    detail = await reach_question(db, settings, client, run)
    await post(
        client,
        f"/v1/courses/{course}/teaching-memories/{accepted['id']}/revoke",
        {"content_hash": accepted["content_hash"], "reason": "后续课次不再复用"},
    )
    assert (await client.get(f"/v1/courses/{course}/teaching-memories")).json()["items"] == []
    # The pinned read receipt is history; revocation does not rewrite prior model inputs.
    assert (await complete(db, settings, client, run, detail))["status"] == "waiting_teacher"
    async with db.sessions() as session:
        row = await session.get(AgentRun, UUID(run["id"]))
        assert row.task_state["memory_snapshot"]["items"] == [accepted]


def test_memory_and_confirmation_tools_are_not_exposed_to_independent_rubric():
    assisted = {
        item["name"] for item in catalog.resolve("lesson-plan-assisted").registry.manifest()
    }
    assert "search_teaching_memory" in assisted and "ask_teacher" in assisted
    assert assisted.isdisjoint(
        {"confirm_teaching_memory", "revoke_teaching_memory", "create_teaching_memory"}
    )
    rubric = {item["name"] for item in catalog.resolve("rubric").registry.manifest()}
    assert rubric == {"read_question", "propose_rubric"}

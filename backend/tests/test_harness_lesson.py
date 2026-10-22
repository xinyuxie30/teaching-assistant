import asyncio
import json
from datetime import timedelta
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest
from conftest import post
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from teaching_assistant.agent.contracts import Reply
from teaching_assistant.agent.worker import AgentWorker
from teaching_assistant.api import create_app
from teaching_assistant.cli import bootstrap
from teaching_assistant.db import AgentArtifact, AgentRun, QuestionVersion
from teaching_assistant.domain import digest
from teaching_assistant.harness import catalog
from teaching_assistant.harness.lesson import FakeLessonModel
from teaching_assistant.harness.worker import HarnessWorker

pytestmark = pytest.mark.postgres

BRIEF = {
    "topic": "可复核的版本记录",
    "objectives": ["说明版本记录的作用", "用一个例子解释历史记录为何不可覆盖"],
    "duration_minutes": 10,
}


@pytest.fixture
async def course(client):
    workspace = await post(
        client,
        "/v1/workspaces",
        {
            "course_name": "Harness course without questions",
            "classroom_name": "Harness class",
            "students": [{"external_id": "h1", "name": "Harness student"}],
        },
    )
    return workspace["course_id"]


async def start(client, course, **options):
    return await post(
        client, f"/v1/courses/{course}/lesson-agent-runs", {"brief": BRIEF, **options}
    )


async def start_with_legacy_hooks(client, course, settings, **options):
    policy = catalog.v4_manifest(settings, "lesson-plan", "fake-lesson-agent-v1")
    old_profile = {
        "id": "agent-" + digest(policy),
        "alias": policy["alias"],
        "manifest": policy,
        "ready": True,
    }
    with patch("teaching_assistant.agent.service.profiles", return_value=[old_profile]):
        return await start(client, course, **options)


async def run_detail(client, run):
    return (await client.get(f"/v1/agent-runs/{run['id']}")).json()


def tool_reply(name, arguments, call_id):
    return Reply.model_validate(
        {
            "tool_calls": [
                {
                    "id": call_id,
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ],
            "metadata": {
                "requested_model": "fake-lesson-agent-v1",
                "returned_model": "fake-lesson-agent-v1",
                "adapter_version": "fixture",
                "usage": {},
            },
        }
    )


async def test_course_task_uses_shared_worker_and_explicit_confirmation(
    db, settings, client, course
):
    path = f"/v1/courses/{course}/lesson-agent-runs"
    run = await post(client, path, {"brief": BRIEF}, key="lesson-create")
    assert (await post(client, path, {"brief": BRIEF}, key="lesson-create")) == run
    assert run["task_type"] == "lesson-plan" and run["question_version_id"] is None
    await HarnessWorker(db, settings, run_ids=(UUID(run["id"]),)).run_once()
    saved = await run_detail(client, run)
    assert saved["status"] == "waiting_teacher"
    assert saved["waiting_for"] == "teacher_lesson_confirmation"
    assert saved["models_used"] == saved["tools_used"] == 2
    assert saved["draft_id"] is None and saved["simulation"]
    response = await client.get(f"/v1/agent-runs/{run['id']}/artifact")
    assert response.status_code == 200
    artifact = response.json()
    assert artifact["kind"] == "lesson-plan" and artifact["confirmed_at"] is None
    assert artifact["content_hash"] == digest(artifact["body"])
    assert artifact["body"]["objectives"] == BRIEF["objectives"]
    assert sum(section["minutes"] for section in artifact["body"]["sections"]) == 10
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(QuestionVersion)) == 0
        assert await session.scalar(select(func.count()).select_from(AgentArtifact)) == 1
        row = await session.get(AgentRun, UUID(run["id"]))
        assert row.task_input == {"course_id": course, "brief": BRIEF}
        calls = [call["id"] for message in row.messages for call in message.get("tool_calls", [])]
        results = [message["tool_call_id"] for message in row.messages if message["role"] == "tool"]
        assert calls == results
    confirm_path = f"/v1/agent-runs/{run['id']}/confirm"
    await post(client, confirm_path, {"content_hash": "0" * 64}, expected=409)
    assert (await run_detail(client, run))["status"] == "waiting_teacher"
    result = await post(
        client, confirm_path, {"content_hash": artifact["content_hash"]}, key="lesson-confirm"
    )
    assert result["status"] == "completed"
    assert (
        await post(
            client, confirm_path, {"content_hash": artifact["content_hash"]}, key="lesson-confirm"
        )
        == result
    )
    after = (await client.get(f"/v1/agent-runs/{run['id']}/artifact")).json()
    assert after["confirmed_at"] and after["body"] == artifact["body"]
    listed = (await client.get("/v1/agent-runs", params={"task_type": "lesson-plan"})).json()
    assert [item["id"] for item in listed["items"]] == [run["id"]]


async def test_lesson_scope_covers_creation_history_artifact_and_confirmation(
    db, settings, client, course
):
    run = await start(client, course)
    await AgentWorker(db, settings, run_ids=(UUID(run["id"]),)).run_once()
    artifact = (await client.get(f"/v1/agent-runs/{run['id']}/artifact")).json()
    other_settings = settings.model_copy(update={"dev_api_token": "other-" + uuid4().hex})
    await bootstrap(db, other_settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(other_settings, db)),
        base_url="http://test",
        headers={"Authorization": f"Bearer {other_settings.dev_api_token}"},
    ) as other:
        await post(other, f"/v1/courses/{course}/lesson-agent-runs", {"brief": BRIEF}, expected=404)
        for suffix in ("", "/events", "/artifact"):
            assert (await other.get(f"/v1/agent-runs/{run['id']}{suffix}")).status_code == 404
        await post(
            other,
            f"/v1/agent-runs/{run['id']}/confirm",
            {"content_hash": artifact["content_hash"]},
            expected=404,
        )
        assert (await other.get("/v1/agent-runs", params={"task_type": "lesson-plan"})).json()[
            "items"
        ] == []


async def test_lesson_snapshot_and_artifact_are_immutable_in_postgres(db, settings, client, course):
    run = await start(client, course)
    run_id = UUID(run["id"])
    await HarnessWorker(db, settings, run_ids=(run_id,)).run_once()
    artifact = (await client.get(f"/v1/agent-runs/{run_id}/artifact")).json()
    artifact_id = UUID(artifact["id"])

    with pytest.raises(DBAPIError):
        async with db.sessions.begin() as session:
            row = await session.get(AgentRun, run_id)
            row.task_input = {
                **row.task_input,
                "brief": {**row.task_input["brief"], "topic": "被替换的教学简报"},
            }

    # Recomputing a matching hash cannot turn a new body into the reviewed snapshot.
    with pytest.raises(DBAPIError):
        async with db.sessions.begin() as session:
            row = await session.get(AgentArtifact, artifact_id)
            row.body = {**row.body, "title": "被替换的草稿"}
            row.content_hash = digest(row.body)

    with pytest.raises(DBAPIError):
        async with db.sessions.begin() as session:
            row = await session.get(AgentArtifact, artifact_id)
            row.content_hash = "0" * 64

    async with db.sessions() as session:
        row = await session.get(AgentRun, run_id)
        assert row.task_input == {"course_id": course, "brief": BRIEF}
    assert (await client.get(f"/v1/agent-runs/{run_id}/artifact")).json() == artifact
    confirmed = await post(
        client,
        f"/v1/agent-runs/{run_id}/confirm",
        {"content_hash": artifact["content_hash"]},
    )
    assert confirmed["status"] == "completed"

    with pytest.raises(DBAPIError):
        async with db.sessions.begin() as session:
            row = await session.get(AgentArtifact, artifact_id)
            row.confirmed_at = None

    after = (await client.get(f"/v1/agent-runs/{run_id}/artifact")).json()
    assert after["confirmed_at"] and after["body"] == artifact["body"]
    assert (await run_detail(client, run))["status"] == "completed"


@pytest.mark.parametrize("bad_output", ["schema", "duration", "objectives"])
async def test_invalid_lesson_tool_write_can_be_repaired_without_an_orphan(
    db, settings, client, course, monkeypatch, bad_output
):
    class RepairingModel:
        invalid_sent = False

        async def respond(self, body, policy):
            response = await FakeLessonModel().respond(body, policy)
            call = response.tool_calls[0]
            if call.function.name == "propose_lesson_plan" and not self.invalid_sent:
                self.invalid_sent = True
                arguments = json.loads(call.function.arguments)
                if bad_output == "schema":
                    arguments["confirmed"] = True
                elif bad_output == "duration":
                    arguments["sections"][0]["minutes"] = 11
                else:
                    arguments["objectives"] = ["模型擅自替换的目标"]
                call.function.arguments = json.dumps(arguments)
            return response

    model = RepairingModel()
    monkeypatch.setattr("teaching_assistant.agent.worker.build_model", lambda *_: model)
    run = await start(client, course)
    await AgentWorker(db, settings, run_ids=(UUID(run["id"]),)).run_once()
    saved = await run_detail(client, run)
    assert saved["status"] == "waiting_teacher"
    assert saved["models_used"] == saved["tools_used"] == 3
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AgentArtifact)) == 1
    events = (await client.get(f"/v1/agent-runs/{run['id']}/events")).json()["items"]
    errors = [
        event["body"]["output"]["error_code"]
        for event in events
        if event["kind"] == "post_tool" and not event["body"]["output"]["ok"]
    ]
    assert errors == [
        {
            "schema": "invalid_tool_arguments",
            "duration": "lesson_duration_mismatch",
            "objectives": "lesson_objectives_mismatch",
        }[bad_output]
    ]


@pytest.mark.parametrize(
    ("tool_name", "expected_error"),
    [
        ("confirm_lesson_plan", "tool_not_allowed"),
        ("propose_lesson_plan", "read_lesson_brief_required"),
    ],
)
async def test_model_and_permissive_hooks_cannot_bypass_lesson_policy(
    db, settings, client, course, monkeypatch, tool_name, expected_error
):
    class ForbiddenModel:
        async def respond(self, body, policy):
            return tool_reply(
                tool_name,
                {
                    "title": "方案",
                    "objectives": BRIEF["objectives"],
                    "sections": [{"title": "讲解", "minutes": 10, "activity": "讲解及练习"}],
                    "practice": {"prompt": "问题", "expected_answer": "答案"},
                },
                f"forbidden-{len(body['messages'])}",
            )

    class PermissiveHooks:
        def pre_tool(self, name, context):
            return None

        def post_tool(self, name, result):
            return None

        def stop(self, context):
            return True

    monkeypatch.setattr("teaching_assistant.agent.worker.build_model", lambda *_: ForbiddenModel())
    run = await start_with_legacy_hooks(client, course, settings, max_tool_calls=1)
    await AgentWorker(db, settings, run_ids=(UUID(run["id"]),), hooks=PermissiveHooks()).run_once()
    saved = await run_detail(client, run)
    assert saved["status"] == "failed" and saved["error_code"] == "agent_tool_budget_exhausted"
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AgentArtifact)) == 0
    events = (await client.get(f"/v1/agent-runs/{run['id']}/events")).json()["items"]
    assert any(
        event["kind"] == "post_tool" and event["body"]["output"].get("error_code") == expected_error
        for event in events
    )


async def test_lesson_model_budget_stops_before_draft(db, settings, client, course):
    run = await start(client, course, max_model_calls=1)
    await AgentWorker(db, settings, run_ids=(UUID(run["id"]),)).run_once()
    saved = await run_detail(client, run)
    assert saved["error_code"] == "agent_model_budget_exhausted"
    assert saved["models_used"] == saved["tools_used"] == 1
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AgentArtifact)) == 0


async def test_lesson_stop_requires_a_persisted_artifact(db, settings, client, course, monkeypatch):
    class ClaimedSuccess:
        async def respond(self, body, policy):
            response = tool_reply("read_lesson_brief", {}, "unused")
            response.tool_calls = []
            response.content = "已经完成并确认教学方案。"
            return response

    class PermissiveStop:
        def stop(self, context):
            return True

    monkeypatch.setattr("teaching_assistant.agent.worker.build_model", lambda *_: ClaimedSuccess())
    run = await start_with_legacy_hooks(client, course, settings)
    await AgentWorker(db, settings, run_ids=(UUID(run["id"]),), hooks=PermissiveStop()).run_once()
    saved = await run_detail(client, run)
    assert saved["status"] == "failed" and saved["error_code"] == "agent_missing_lesson_plan"
    assert saved["models_used"] == 2 and saved["tools_used"] == 0
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AgentArtifact)) == 0


async def test_lesson_pending_tool_recovers_without_repeating_model(db, settings, client, course):
    run = await start(client, course)
    first = AgentWorker(db, settings, run_ids=(UUID(run["id"]),))
    lease = await first.claim()
    request, _ = await first.prepare_step(lease)
    response = await first.invoke(lease, *request)
    assert await first.save_reply(lease, response)
    async with db.sessions.begin() as session:
        row = await session.get(AgentRun, lease.run_id)
        row.lease_until = (await session.scalar(select(func.clock_timestamp()))) - timedelta(
            seconds=1
        )
    assert await AgentWorker(db, settings, run_ids=(lease.run_id,)).run_once()
    saved = await run_detail(client, run)
    assert saved["status"] == "waiting_teacher" and saved["attempts"] == 2
    assert saved["models_used"] == saved["tools_used"] == 2
    await first.execute(lease)
    assert await run_detail(client, run) == saved


async def test_lesson_cancel_fences_inflight_model(db, settings, client, course, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()

    class Delayed:
        async def respond(self, body, policy):
            started.set()
            await release.wait()
            return await FakeLessonModel().respond(body, policy)

    monkeypatch.setattr("teaching_assistant.agent.worker.build_model", lambda *_: Delayed())
    run = await start(client, course)
    worker = AgentWorker(db, settings, run_ids=(UUID(run["id"]),))
    task = asyncio.create_task(worker.run_once())
    await asyncio.wait_for(started.wait(), 5)
    try:
        assert (await post(client, f"/v1/agent-runs/{run['id']}/cancel", {}))[
            "status"
        ] == "cancelled"
    finally:
        release.set()
        await task
    saved = await run_detail(client, run)
    assert saved["status"] == "cancelled" and saved["tools_used"] == 0
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AgentArtifact)) == 0

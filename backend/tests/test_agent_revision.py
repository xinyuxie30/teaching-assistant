"""Teacher-directed revisions preserve earlier artifacts and use frozen inputs."""

import json
from copy import deepcopy
from uuid import UUID, uuid4

import httpx
import pytest
from alembic import command
from alembic.config import Config
from conftest import post
from sqlalchemy import func, select, text
from teaching_assistant.agent.contracts import RunInput
from teaching_assistant.agent.models import FakeAgentModel
from teaching_assistant.agent.worker import AgentWorker
from teaching_assistant.api import create_app
from teaching_assistant.cli import bootstrap
from teaching_assistant.db import AgentRun, Receipt, RubricDraft, RubricVersion
from teaching_assistant.domain import digest

pytestmark = pytest.mark.postgres


async def read(client, run_id):
    response = await client.get(f"/v1/agent-runs/{run_id}")
    assert response.status_code == 200
    return response.json()


@pytest.mark.parametrize("confirm_source", [False, True])
async def test_revision_creates_separate_draft_and_confirmation(
    db, settings, client, scenario, monkeypatch, confirm_source
):
    path = f"/v1/question-versions/{scenario['question']['id']}/rubric-agent-runs"
    parent = await post(client, path, {})
    await AgentWorker(db, settings, run_ids=(UUID(parent["id"]),)).run_once()
    parent = await read(client, parent["id"])
    if confirm_source:
        await post(
            client, f"/v1/rubric-drafts/{parent['draft_id']}/confirm", {"expected_revision": 1}
        )
        parent = await read(client, parent["id"])
    original = (await client.get(f"/v1/rubric-drafts/{parent['draft_id']}")).json()
    revised_body = deepcopy(original["body"])
    revised_body["criteria"][0]["description"] = "老师要求澄清的独立评分点"
    feedback = "请澄清第一个评分点的描述，保留分值。"
    seen = []

    class RevisionModel(FakeAgentModel):
        async def respond(self, body, policy):
            source = json.loads(body["messages"][1]["content"].split("\n", 1)[1])
            assert source["source_rubric"] == original["body"]
            assert source["teacher_feedback"] == feedback
            assert source["source_content_hash"] == digest(original["body"])
            seen.append(source)
            response = await super().respond(body, policy)
            if response.tool_calls[0].function.name == "propose_rubric":
                response.tool_calls[0].function.arguments = json.dumps({"rubric": revised_body})
            return response

    monkeypatch.setattr("teaching_assistant.agent.worker.build_model", lambda *_: RevisionModel())
    request = {"source_draft_id": parent["draft_id"], "instructions": feedback}
    run = await post(client, path, request, key="revise-once")
    assert run["models_used"] == run["tools_used"] == 0
    assert run["source_draft"] == {
        "id": parent["draft_id"],
        "revision": 1,
        "content_hash": digest(original["body"]),
    }
    await AgentWorker(db, settings, run_ids=(UUID(run["id"]),)).run_once()
    saved = await read(client, run["id"])
    assert saved["status"] == "waiting_teacher" and len(seen) == 2
    assert saved["revision_feedback"] == feedback
    assert saved["draft_id"] != parent["draft_id"]
    draft = (await client.get(f"/v1/rubric-drafts/{saved['draft_id']}")).json()
    assert draft["body"] == revised_body and draft["confirmed_version_id"] is None
    assert await post(client, path, request, key="revise-once") == run
    conflict = await post(
        client, path, {**request, "instructions": "另一条意见"}, key="revise-once", expected=409
    )
    assert conflict["code"] == "idempotency_conflict"
    conflict = await post(
        client,
        path,
        {**request, "source_draft_id": scenario["draft"]["id"]},
        key="revise-once",
        expected=409,
    )
    assert conflict["code"] == "idempotency_conflict"
    await post(client, f"/v1/rubric-drafts/{saved['draft_id']}/confirm", {"expected_revision": 1})
    assert (await read(client, run["id"]))["status"] == "completed"
    assert await read(client, parent["id"]) == parent
    assert (await client.get(f"/v1/rubric-drafts/{parent['draft_id']}")).json() == original
    history = (await client.get("/v1/agent-runs")).json()["items"]
    assert len(history) == 2
    assert all("revision_feedback" not in item and "messages" not in item for item in history)
    assert (
        next(item for item in history if item["id"] == run["id"])["source_draft"]
        == run["source_draft"]
    )
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(RubricVersion)) == 2 + int(
            confirm_source
        )


async def test_revision_uses_saved_snapshot_even_if_source_changes_and_can_cancel(
    db, settings, client, scenario
):
    path = f"/v1/question-versions/{scenario['question']['id']}/rubric-agent-runs"
    request = {"source_draft_id": scenario["draft"]["id"], "instructions": "补充部分得分描述"}
    run = await post(client, path, request)
    async with db.sessions.begin() as session:
        source = await session.get(RubricDraft, UUID(scenario["draft"]["id"]))
        original = deepcopy(source.body)
        # Simulate a later editor; the run must never reread mutable draft content.
        source.body = {**source.body, "global_instructions": "后来编辑的内容"}
        source.revision += 1
    worker = AgentWorker(db, settings, run_ids=(UUID(run["id"]),))
    lease = await worker.claim()
    async with db.sessions() as session:
        saved = await session.get(AgentRun, lease.run_id)
        assert saved.revision_context["source_rubric"] == original
        assert saved.revision_context["source_revision"] == 1
        prompt = json.loads(saved.messages[1]["content"].split("\n", 1)[1])
        assert prompt["source_rubric"] == original
    cancelled = await post(client, f"/v1/agent-runs/{run['id']}/cancel", {})
    await worker.execute(lease)
    assert cancelled["status"] == "cancelled" and cancelled["draft_id"] is None
    assert await read(client, run["id"]) == {
        **cancelled,
        "revision_feedback": request["instructions"],
    }
    assert (await client.get(f"/v1/rubric-versions/{scenario['rubric']['id']}")).json()[
        "body"
    ] == original


async def test_revision_source_cannot_cross_question_or_teacher(db, settings, client, scenario):
    request = {"source_draft_id": scenario["draft"]["id"], "instructions": "修改等级描述"}
    question = await post(
        client,
        f"/v1/assignments/{scenario['assignment']['id']}/question-versions",
        {
            "question_key": "q2",
            "prompt": "另一题",
            "reference": "其他参考",
            "maximum_points": "10.00",
        },
    )
    path = f"/v1/question-versions/{question['id']}/rubric-agent-runs"
    assert (await post(client, path, request, expected=404))["code"] == "not_found"
    other_settings = settings.model_copy(update={"dev_api_token": "other-" + uuid4().hex})
    await bootstrap(db, other_settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(other_settings, db)),
        base_url="http://test",
        headers={"Authorization": f"Bearer {other_settings.dev_api_token}"},
    ) as other:
        await post(other, path, request, expected=404)
        workspace = await post(
            other,
            "/v1/workspaces",
            {
                "course_name": "Other course",
                "classroom_name": "Other class",
                "students": [{"external_id": "other", "name": "Other student"}],
            },
        )
        assignment = await post(
            other,
            f"/v1/courses/{workspace['course_id']}/assignments",
            {
                "classroom_id": workspace["classroom_id"],
                "title": "Other assignment",
            },
        )
        owned = await post(
            other,
            f"/v1/assignments/{assignment['id']}/question-versions",
            {
                "question_key": "q1",
                "prompt": "Owned",
                "reference": "Reference",
                "maximum_points": "10.00",
            },
        )
        await post(
            other, f"/v1/question-versions/{owned['id']}/rubric-agent-runs", request, expected=404
        )
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AgentRun)) == 0


async def test_revision_feedback_and_full_snapshot_are_preflighted(db, client, scenario):
    path = f"/v1/question-versions/{scenario['question']['id']}/rubric-agent-runs"
    request = {"source_draft_id": scenario["draft"]["id"]}
    await post(client, path, request, expected=422)
    await post(client, path, {**request, "instructions": " \n\t"}, expected=422)
    await post(client, path, {**request, "instructions": "字" * 4001}, expected=422)
    async with db.sessions.begin() as session:
        source = await session.get(RubricDraft, UUID(scenario["draft"]["id"]))
        source.body = {**source.body, "global_instructions": "原稿材料" * 2000}
    error = await post(
        client,
        path,
        {
            **request,
            "instructions": "简化描述",
            "max_context_characters": 7000,
        },
        expected=422,
    )
    assert error["code"] == "agent_context_budget_exceeded"
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AgentRun)) == 0


async def test_migration_preserves_legacy_runs_and_creation_receipts(db, client, scenario):
    path = f"/v1/question-versions/{scenario['question']['id']}/rubric-agent-runs"
    run = await post(client, path, {}, key="legacy-create")
    old_payload = RunInput().model_dump(mode="json", exclude={"source_draft_id"}, exclude_none=True)
    async with db.sessions() as session:
        receipt = await session.scalar(select(Receipt).where(Receipt.key == "legacy-create"))
        assert receipt.request_hash == digest(old_payload)

    def round_trip(connection):
        config = Config("alembic.ini")
        config.attributes["connection"] = connection
        command.downgrade(config, "0005")
        command.upgrade(config, "head")
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0013"
        command.check(config)

    async with db.engine.begin() as connection:
        await connection.run_sync(round_trip)
    assert await read(client, run["id"]) == run
    assert await post(client, path, {"source_draft_id": None}, key="legacy-create") == run

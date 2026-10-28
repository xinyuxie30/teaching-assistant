"""Real DB evidence isolation, retrieval grounding, snapshots and teacher confirmation."""

import json
from uuid import UUID, uuid4

import pytest
from conftest import post
from sqlalchemy import select, text
from teaching_assistant.db import AgentArtifact, AgentRun, Classroom, PracticeQuestion
from teaching_assistant.harness.class_practice import FakeClassPracticeModel
from teaching_assistant.harness.worker import HarnessWorker
from test_analysis import review
from test_harness_assisted_memory import knowledge
from test_workflow import drain

pytestmark = pytest.mark.postgres

QUESTION = {
    "title": "固定评分规则的意义",
    "prompt": "为什么要用统一固定规则评估，并保留历史版本？",
    "expected_answer": "保证一致性，并能追溯和复核历史结果。",
    "knowledge_points": ["统一规则", "历史追溯"],
    "grade": "七年级",
    "difficulty": "basic",
    "question_type": "简答题",
    "source": "教师自编的合成测试题",
}


async def test_practice_migration_roundtrip_matches_models(db):
    from alembic import command
    from alembic.config import Config

    def check(connection):
        config = Config("alembic.ini")
        config.attributes["connection"] = connection
        command.downgrade(config, "0012")
        command.upgrade(config, "head")
        command.check(config)

    async with db.engine.begin() as connection:
        await connection.run_sync(check)


async def add_question(client, course, *, confirmed=True, **changes):
    path = f"/v1/courses/{course}/practice-questions"
    question = (await post(client, path, {"questions": [{**QUESTION, **changes}]}))["items"][0]
    if confirmed:
        question = await post(
            client, f"{path}/{question['id']}/confirm", {"content_hash": question["content_hash"]}
        )
    return question


@pytest.fixture
async def prepared(client, db, settings, scenario):
    await drain(db, settings)
    await post(
        client,
        f"/v1/grading-items/{scenario['batch']['item_ids'][0]}/reviews",
        review("missing", "missing"),
    )
    course = scenario["workspace"]["course_id"]
    question = await add_question(client, course)
    memory = await knowledge(client, course, title="统一规则与历史追溯")
    path = f"/v1/classrooms/{scenario['workspace']['classroom_id']}/practice-runs"
    request = {"batch_ids": [scenario["batch"]["id"]], "question_count": 1, "grade": "七年级"}
    return {
        "path": path,
        "request": request,
        "question": question,
        "memory": memory,
        "course": course,
    }


async def start(client, prepared, **changes):
    preview = await post(client, prepared["path"] + "/preview", prepared["request"])
    run = await post(
        client,
        prepared["path"],
        {**prepared["request"], "preview_hash": preview["preview_hash"], **changes},
    )
    return preview, run


async def test_class_practice_complete_grounded_workflow(client, db, settings, scenario, prepared):
    preview, run = await start(client, prepared)
    assert preview["candidates"][0]["question"]["id"] == prepared["question"]["id"]
    assert preview["batches"][0]["coverage"]["confirmed"] == 1
    assert preview["batches"][0]["coverage"]["pending_review"] == 1
    assert all(e["confirmed_students"] == 1 for e in preview["evidence"])
    assert preview["memories"] == [prepared["memory"]]
    for student in scenario["workspace"]["students"]:
        assert student["id"] not in json.dumps(preview)
        assert student["name"] not in json.dumps(preview)
    # Stored tasks survive a new Worker and preserve exactly the chosen source contents.
    assert await HarnessWorker(db, settings, run_ids=(UUID(run["id"]),)).run_once()
    detail = (await client.get(f"/v1/agent-runs/{run['id']}")).json()
    assert detail["status"] == "waiting_teacher", detail
    artifact = (await client.get(f"/v1/agent-runs/{run['id']}/artifact")).json()
    assert artifact["body"]["questions"][0]["question"] == prepared["question"]
    assert artifact["body"]["memories"] == [prepared["memory"]]
    assert artifact["confirmed_at"] is None and artifact["body"]["simulation"] is True
    # No model call is required for explicit teacher confirmation.
    confirmed = await post(
        client, f"/v1/agent-runs/{run['id']}/confirm", {"content_hash": artifact["content_hash"]}
    )
    assert confirmed["status"] == "completed" and confirmed["models_used"] == 2
    assert not await HarnessWorker(db, settings, run_ids=(UUID(run["id"]),)).run_once()
    assert (await client.get(prepared["path"])).json()["items"][0]["id"] == run["id"]


async def test_preview_is_stable_and_stale_review_is_rejected(client, scenario, prepared):
    first = await post(client, prepared["path"] + "/preview", prepared["request"])
    second = await post(client, prepared["path"] + "/preview", prepared["request"])
    assert first["preview_hash"] == second["preview_hash"]
    await post(
        client,
        f"/v1/grading-items/{scenario['batch']['item_ids'][0]}/reviews",
        review("complete", "missing", revision=1),
    )
    error = await post(
        client,
        prepared["path"],
        {**prepared["request"], "preview_hash": first["preview_hash"]},
        expected=409,
    )
    assert error["code"] == "practice_preview_changed"


async def test_no_unreviewed_evidence_and_no_invented_candidates(client, db, settings, scenario):
    path = f"/v1/classrooms/{scenario['workspace']['classroom_id']}/practice-runs"
    request = {"batch_ids": [scenario["batch"]["id"]], "question_count": 1}
    await drain(db, settings)
    assert (await post(client, path + "/preview", request, expected=409))[
        "code"
    ] == "practice_no_evidence"
    await post(
        client,
        f"/v1/grading-items/{scenario['batch']['item_ids'][0]}/reviews",
        review("missing", "missing"),
    )
    preview = await post(client, path + "/preview", request)
    assert preview["candidates"] == []
    assert (
        await post(client, path, {**request, "preview_hash": preview["preview_hash"]}, expected=409)
    )["code"] == "practice_not_enough_questions"


async def test_retrieval_filters_unconfirmed_retired_unrelated_and_foreign(client, prepared):
    course = prepared["course"]
    await add_question(client, course, confirmed=False, title="待确认的统一规则")
    retired = await add_question(client, course, title="停用的统一规则")
    await post(
        client,
        f"/v1/courses/{course}/practice-questions/{retired['id']}/retire",
        {"content_hash": retired["content_hash"]},
    )
    await add_question(client, course, title="不同年级统一规则", grade="八年级")
    await add_question(client, course, title="加法", prompt="1+1?", knowledge_points=["加法"])
    other = await post(
        client,
        "/v1/workspaces",
        {
            "course_name": "Other",
            "classroom_name": "Other",
            "students": [{"external_id": "x", "name": "Synthetic"}],
        },
    )
    await add_question(client, other["course_id"])
    result = await post(client, prepared["path"] + "/preview", prepared["request"])
    assert [c["question"]["id"] for c in result["candidates"]] == [prepared["question"]["id"]]
    # Changed bank eligibility invalidates a prior preview.
    await post(
        client,
        f"/v1/courses/{course}/practice-questions/{prepared['question']['id']}/retire",
        {"content_hash": prepared["question"]["content_hash"]},
    )
    assert (
        await post(
            client,
            prepared["path"],
            {**prepared["request"], "preview_hash": result["preview_hash"]},
            expected=409,
        )
    )["code"] == "practice_preview_changed"


async def test_scope_idempotency_and_duplicate_question_batches(client, db, scenario, prepared):
    async with db.sessions.begin() as session:
        other = Classroom(course_id=UUID(prepared["course"]), name="Different classroom")
        session.add(other)
        await session.flush()
        other_id = other.id
    assert (
        await post(
            client,
            f"/v1/classrooms/{other_id}/practice-runs/preview",
            prepared["request"],
            expected=422,
        )
    )["code"] == "practice_scope_mismatch"
    duplicate = await post(client, "/v1/grading-batches", scenario["batch_payload"])
    assert (
        await post(
            client,
            prepared["path"] + "/preview",
            {**prepared["request"], "batch_ids": [scenario["batch"]["id"], duplicate["id"]]},
            expected=422,
        )
    )["code"] == "practice_duplicate_question"
    preview = await post(client, prepared["path"] + "/preview", prepared["request"])
    payload = {**prepared["request"], "preview_hash": preview["preview_hash"]}
    first = await post(client, prepared["path"], payload, key="same-practice")
    assert await post(client, prepared["path"], payload, key="same-practice") == first
    assert (
        await post(
            client,
            prepared["path"],
            {**payload, "instructions": "changed"},
            key="same-practice",
            expected=409,
        )
    )["code"] == "idempotency_conflict"


async def test_teacher_selection_and_unknown_source_rejected(
    client, db, settings, prepared, monkeypatch
):
    other = await add_question(client, prepared["course"], title="历史版本追溯练习二")
    preview, run = await start(client, prepared, selected_question_ids=[other["id"]])
    async with db.sessions() as session:
        stored = await session.get(AgentRun, UUID(run["id"]))
        assert [c["question"]["id"] for c in stored.task_input["candidates"]] == [other["id"]]
    assert (
        await post(
            client,
            prepared["path"],
            {
                **prepared["request"],
                "preview_hash": preview["preview_hash"],
                "selected_question_ids": [str(uuid4())],
            },
            expected=422,
        )
    )["code"] == "practice_invalid_selection"

    class Forged(FakeClassPracticeModel):
        async def respond(self, body, policy):
            result = await super().respond(body, policy)
            call = result.tool_calls[0]
            if call.function.name == "propose_class_practice":
                data = json.loads(call.function.arguments)
                data["selections"][0]["question_id"] = str(uuid4())
                call.function.arguments = json.dumps(data)
            return result

    monkeypatch.setattr("teaching_assistant.harness.worker.build_model", lambda *_: Forged())
    assert await HarnessWorker(db, settings, run_ids=(UUID(run["id"]),)).run_once()
    async with db.sessions() as session:
        assert (
            await session.scalar(
                select(AgentArtifact).where(AgentArtifact.run_id == UUID(run["id"]))
            )
            is None
        )
    assert (await client.get(f"/v1/agent-runs/{run['id']}")).json()["status"] == "failed"


async def test_frozen_run_survives_later_source_changes(client, db, settings, scenario, prepared):
    preview, run = await start(client, prepared)
    await post(
        client,
        f"/v1/grading-items/{scenario['batch']['item_ids'][0]}/reviews",
        review("complete", "complete", revision=1),
    )
    memory = prepared["memory"]
    await post(
        client,
        f"/v1/courses/{prepared['course']}/teaching-memories/{memory['id']}/revoke",
        {"content_hash": memory["content_hash"], "reason": "不适用于后续备课"},
    )
    assert await HarnessWorker(db, settings, run_ids=(UUID(run["id"]),)).run_once()
    artifact = (await client.get(f"/v1/agent-runs/{run['id']}/artifact")).json()
    assert artifact["body"]["evidence"] == preview["evidence"]
    assert artifact["body"]["memories"] == preview["memories"]


async def test_bank_deduplicates_and_database_protects_contents(client, db, prepared):
    duplicate = await add_question(client, prepared["course"])
    assert duplicate["id"] == prepared["question"]["id"]
    async with db.sessions() as session:
        row = await session.get(PracticeQuestion, UUID(duplicate["id"]))
        assert row.body["prompt"] == QUESTION["prompt"]
    from sqlalchemy.exc import DBAPIError

    with pytest.raises(DBAPIError):
        async with db.sessions.begin() as session:
            await session.execute(
                text("UPDATE practice_questions SET body = '{}' WHERE id = :id"),
                {"id": UUID(duplicate["id"])},
            )


async def test_unauthorized_teacher_cannot_see_bank_or_practice(client, db, settings, prepared):
    import httpx
    from teaching_assistant.api import create_app
    from teaching_assistant.cli import bootstrap
    from teaching_assistant.settings import Settings

    other = Settings(dev_api_token="foreign-teacher-" + uuid4().hex)
    await bootstrap(db, other)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(settings, db)),
        base_url="http://test",
        headers={"Authorization": f"Bearer {other.dev_api_token}"},
    ) as foreign:
        assert (
            await foreign.get(f"/v1/courses/{prepared['course']}/practice-questions")
        ).status_code == 404
        assert (await foreign.get(prepared["path"])).status_code == 404
        assert (
            await foreign.post(prepared["path"] + "/preview", json=prepared["request"])
        ).status_code == 404

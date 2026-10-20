import asyncio
import copy
from uuid import UUID, uuid4

import httpx
import pytest
from conftest import post
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError
from teaching_assistant.api import create_app
from teaching_assistant.cli import bootstrap
from teaching_assistant.db import (
    AnswerVersion,
    Assessment,
    GradingItem,
    ModelCall,
    Review,
    Step,
)
from teaching_assistant.demo import DEMO_RUBRIC
from teaching_assistant.settings import Settings
from teaching_assistant.worker import Worker

pytestmark = pytest.mark.postgres


def review_payload(*, revision=0, level="complete"):
    return {
        "expected_revision": revision,
        "decisions": [
            {
                "criterion_id": c["id"],
                "level_id": level,
                "evidence_support": "supported",
                "reason": "Teacher inspected this evidence and confirmed the level.",
            }
            for c in DEMO_RUBRIC["criteria"]
        ],
    }


async def drain(db, settings):
    worker = Worker(db, settings)
    while await worker.run_once():
        pass


async def test_full_chain_preserves_pending_and_export_snapshot(client, db, settings, scenario):
    await drain(db, settings)
    batch_id = scenario["batch"]["id"]
    state = (await client.get(f"/v1/grading-batches/{batch_id}")).json()
    assert state["status"] == "settled" and state["counts"]["succeeded"] == 2
    item_id = scenario["batch"]["item_ids"][0]
    before = (await client.get(f"/v1/grading-items/{item_id}")).json()
    assert before["simulation"] and before["confirmed_score"] is None
    assert before["answer"]["text"] in [r["answer_text"] for r in scenario["rows"]]
    for assessment in before["assessments"]:
        evidence = assessment["evidence"]
        for quote in evidence["citations"]:
            assert before["answer"]["text"][quote["start"] : quote["end"]] == quote["quote"]
        assert evidence["answer_hash"] == before["answer"]["content_hash"]
    reviewed = await post(client, f"/v1/grading-items/{item_id}/reviews", review_payload())
    assert reviewed["confirmed_score"] == "10.00"
    exported = await post(client, f"/v1/grading-batches/{batch_id}/exports", {"mode": "working"})
    snapshot = exported["snapshot"]
    assert snapshot["counts"]["confirmed"] == 1 and snapshot["counts"]["not_confirmed"] == 1
    assert sum(row["confirmed_score"] is None for row in snapshot["rows"]) == 1
    await post(
        client, f"/v1/grading-items/{item_id}/reviews", review_payload(revision=1, level="missing")
    )
    retrieved = (await client.get(f"/v1/exports/{exported['id']}")).json()
    assert retrieved["snapshot"] == snapshot
    confirmed_only = await post(
        client,
        f"/v1/grading-batches/{batch_id}/exports",
        {
            "mode": "confirmed_only",
        },
    )
    assert confirmed_only["snapshot"]["counts"]["excluded"] == 1
    assert confirmed_only["snapshot"]["rows"][0]["confirmed_score"] == "0.00"
    csv_response = await client.get(f"/v1/exports/{exported['id']}?format=csv")
    assert "'=unsafe-csv-name" in csv_response.text
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Review)) == 2
        assert await session.scalar(select(func.count()).select_from(Assessment)) == 4


async def test_idempotency_and_cross_batch_target_dedup(client, db, scenario):
    key = uuid4().hex
    payload = scenario["batch_payload"]
    responses = await asyncio.gather(
        *[
            client.post("/v1/grading-batches", json=payload, headers={"Idempotency-Key": key})
            for _ in range(3)
        ]
    )
    assert all(r.status_code == 202 for r in responses)
    assert len({r.json()["id"] for r in responses}) == 1
    assert set(responses[0].json()["item_ids"]) == set(scenario["batch"]["item_ids"])
    changed = {**payload, "answer_version_ids": payload["answer_version_ids"][:1]}
    conflict = await post(client, "/v1/grading-batches", changed, key=key, expected=409)
    assert conflict["code"] == "idempotency_conflict"
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(GradingItem)) == 2


async def test_import_is_atomic_and_source_revisions_do_not_overwrite(client, db, scenario):
    path = f"/v1/assignments/{scenario['assignment']['id']}/imports"
    replay = await post(client, path, {"rows": scenario["rows"]})
    assert replay["reused"] == 2
    changed = copy.deepcopy(scenario["rows"])
    changed[0]["answer_text"] = "changed"
    await post(client, path, {"rows": changed}, expected=409)
    changed[0]["source_revision"] = "correction-1"
    changed[1]["student_id"] = str(uuid4())
    await post(client, path, {"rows": changed}, expected=422)
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AnswerVersion)) == 2
    new = await post(client, path, {"rows": changed[:1]})
    assert new["answer_version_ids"][0] not in scenario["imported"]["answer_version_ids"]


async def test_review_concurrency_and_repeat_request(client, db, settings, scenario):
    await drain(db, settings)
    item_id = scenario["batch"]["item_ids"][0]
    path = f"/v1/grading-items/{item_id}/reviews"
    first_key, second_key = uuid4().hex, uuid4().hex
    responses = await asyncio.gather(
        client.post(path, json=review_payload(), headers={"Idempotency-Key": first_key}),
        client.post(path, json=review_payload(), headers={"Idempotency-Key": second_key}),
    )
    assert sorted(r.status_code for r in responses) == [200, 409]
    winning_key = first_key if responses[0].status_code == 200 else second_key
    replay = await post(client, path, review_payload(), key=winning_key)
    assert replay["revision"] == 1
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Review)) == 1


async def test_teacher_scope_cannot_be_supplied_by_client(client, db, scenario):
    other_settings = Settings(dev_api_token=f"second-teacher-{uuid4().hex}")
    await bootstrap(db, other_settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(other_settings, db)),
        base_url="http://test",
        headers={"Authorization": f"Bearer {other_settings.dev_api_token}"},
    ) as other:
        response = await other.get(f"/v1/grading-items/{scenario['batch']['item_ids'][0]}")
        assert response.status_code == 404
        await post(other, "/v1/grading-batches", scenario["batch_payload"], expected=404)
        await post(
            other,
            f"/v1/assignments/{scenario['assignment']['id']}/imports",
            {
                "rows": scenario["rows"],
            },
            expected=404,
        )
    forged = await client.post(
        "/v1/workspaces",
        json={
            "teacher_id": str(uuid4()),
            "course_name": "x",
            "classroom_name": "y",
            "students": [{"external_id": "s1", "name": "Student"}],
        },
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert forged.status_code == 422


async def test_new_rubric_creates_new_result_and_preserves_old(client, db, settings, scenario):
    await drain(db, settings)
    modified = copy.deepcopy(DEMO_RUBRIC)
    modified["criteria"][0]["levels"][1]["points"] = "2.00"
    draft = await post(
        client,
        f"/v1/question-versions/{scenario['question']['id']}/rubric-drafts",
        {
            "rubric": modified,
        },
    )
    rubric = await post(
        client, f"/v1/rubric-drafts/{draft['id']}/confirm", {"expected_revision": 1}
    )
    assert rubric["version"] == 2
    batch = await post(
        client,
        "/v1/grading-batches",
        {
            "rubric_version_id": rubric["id"],
            "answer_version_ids": scenario["imported"]["answer_version_ids"][:1],
        },
    )
    assert not set(batch["item_ids"]) & set(scenario["batch"]["item_ids"])
    await drain(db, settings)
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(GradingItem)) == 3
        assert await session.scalar(select(func.count()).select_from(Assessment)) == 6


async def test_database_rejects_modification_of_frozen_answers(db, scenario):
    async with db.sessions() as session:
        with pytest.raises(DBAPIError):
            await session.execute(
                update(AnswerVersion)
                .where(AnswerVersion.id == UUID(scenario["imported"]["answer_version_ids"][0]))
                .values(answer_text="tampered")
            )
        await session.rollback()


async def test_concurrent_workers_only_accept_one_result_per_point(db, settings, scenario):
    workers = [Worker(db, settings) for _ in range(3)]
    await asyncio.gather(*(w.run_once() for w in workers))
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Assessment)) == 4
        assert await session.scalar(select(func.count()).select_from(Step)) == 8
        assert await session.scalar(select(func.count()).select_from(ModelCall)) == 8
        assert all(
            state == "succeeded" for state in await session.scalars(select(GradingItem.status))
        )

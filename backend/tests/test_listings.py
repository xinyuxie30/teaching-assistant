"""Promoted local checks: independent fixtures, real PostgreSQL, no provider network."""

import csv
import io
from uuid import uuid4

import httpx
import pytest
from conftest import post
from teaching_assistant.api import create_app
from teaching_assistant.cli import bootstrap
from teaching_assistant.domain import Evaluation
from teaching_assistant.evaluators import FakeEvaluator
from teaching_assistant.worker import Worker
from test_workflow import drain, review_payload

pytestmark = pytest.mark.postgres


async def listing(client, path, **params):
    response = await client.get(path, params=params)
    assert response.status_code == 200, response.text
    return response.json()


async def test_keyset_scope_and_literal_search(client, scenario):
    for name in ["prefix 100%_literal", "prefix ordinary"]:
        await post(
            client,
            "/v1/workspaces",
            {
                "course_name": name,
                "classroom_name": "Another class",
                "students": [{"external_id": "s1", "name": "Student"}],
            },
        )
    first = await listing(client, "/v1/courses", limit=1)
    assert first["total"] == 3 and first["has_more"]
    second = await listing(client, "/v1/courses", limit=1, cursor=first["next_cursor"])
    third = await listing(client, "/v1/courses", limit=1, cursor=second["next_cursor"])
    assert len({page["items"][0]["id"] for page in [first, second, third]}) == 3
    assert third["has_more"] is False and third["next_cursor"] is None
    exact = await listing(client, "/v1/courses", q="%_")
    assert [item["name"] for item in exact["items"]] == ["prefix 100%_literal"]
    mismatched = await client.get(
        "/v1/courses", params={"cursor": first["next_cursor"], "q": "prefix"}
    )
    assert mismatched.status_code == 422 and mismatched.json()["code"] == "invalid_cursor"
    malformed = await client.get("/v1/courses", params={"cursor": "not-a-cursor"})
    assert malformed.status_code == 422
    invalid_dates = await client.get(
        "/v1/courses",
        params={"created_from": "2026-10-21T00:00:00Z", "created_to": "2026-10-20T00:00:00Z"},
    )
    assert invalid_dates.status_code == 422
    boundary = first["items"][0]["created_at"]
    at_boundary = await listing(client, "/v1/courses", created_from=boundary, created_to=boundary)
    assert first["items"][0]["id"] in {row["id"] for row in at_boundary["items"]}


async def test_parent_scope_and_cross_teacher_cursors(client, db, settings, scenario):
    course_id = scenario["workspace"]["course_id"]
    own_students = await listing(
        client,
        f"/v1/courses/{course_id}/students",
        classroom_id=scenario["workspace"]["classroom_id"],
    )
    assert {s["id"] for s in own_students["items"]} == {
        s["id"] for s in scenario["workspace"]["students"]
    }
    item_page = await listing(client, "/v1/grading-items", limit=1)
    other_settings = settings.model_copy(update={"dev_api_token": f"other-{uuid4().hex}"})
    await bootstrap(db, other_settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(other_settings, db)),
        base_url="http://test",
        headers={"Authorization": f"Bearer {other_settings.dev_api_token}"},
    ) as other:
        for path in ["/v1/courses", "/v1/grading-batches", "/v1/grading-items", "/v1/exports"]:
            page = await listing(other, path)
            assert page["total"] == 0 and page["items"] == []
        for path, params in [
            (f"/v1/courses/{course_id}/students", {}),
            (f"/v1/courses/{course_id}/assignments", {}),
            (f"/v1/assignments/{scenario['assignment']['id']}", {}),
            ("/v1/grading-items", {"batch_id": scenario["batch"]["id"]}),
            ("/v1/grading-batches", {"course_id": course_id}),
            (f"/v1/grading-items/{scenario['batch']['item_ids'][0]}", {}),
            (f"/v1/grading-batches/{scenario['batch']['id']}", {}),
        ]:
            assert (await other.get(path, params=params)).status_code == 404
        reused_cursor = await other.get(
            "/v1/grading-items", params={"cursor": item_page["next_cursor"], "limit": 1}
        )
        assert reused_cursor.status_code == 422
        assert reused_cursor.json()["code"] == "invalid_cursor"
    wrong_route = await client.get(
        "/v1/grading-batches", params={"cursor": item_page["next_cursor"]}
    )
    assert wrong_route.status_code == 422


async def test_workbench_headers_and_current_review_filter(client, db, settings, scenario):
    await drain(db, settings)
    batch_id, item_id = scenario["batch"]["id"], scenario["batch"]["item_ids"][0]
    item = (await client.get(f"/v1/grading-items/{item_id}")).json()
    batch = (await client.get(f"/v1/grading-batches/{batch_id}")).json()
    assert item["assignment"]["id"] == batch["assignment"]["id"] == scenario["assignment"]["id"]
    assert item["student"]["id"] in {s["id"] for s in scenario["workspace"]["students"]}
    assert item["question"]["question_key"] == batch["question"]["question_key"] == "q1"
    assert batch["rubric_version"] == item["rubric"]["version"] == 1
    assert batch["created_at"]
    ready = await listing(
        client, "/v1/grading-items", batch_id=batch_id, status="succeeded", review_status="pending"
    )
    assert ready["total"] == 2 and all(row["needs_review"] for row in ready["items"])
    await post(client, f"/v1/grading-items/{item_id}/reviews", review_payload())
    snapshot = await post(client, f"/v1/grading-batches/{batch_id}/exports", {"mode": "working"})
    amended = review_payload(revision=1)
    amended["decisions"][0]["level_id"] = "partial"
    await post(client, f"/v1/grading-items/{item_id}/reviews", amended)
    confirmed = await listing(
        client, "/v1/grading-items", batch_id=batch_id, review_status="confirmed"
    )
    assert confirmed["total"] == 1
    assert confirmed["items"][0]["confirmed_score"] == "7.00"
    assert confirmed["items"][0]["review_revision"] == 2
    assert confirmed["items"][0]["needs_review"] is False
    exports = await listing(client, "/v1/exports", batch_id=batch_id, mode="working")
    assert exports["items"][0]["counts"]["confirmed"] == 1
    assert "snapshot" not in exports["items"][0] and "rows" not in exports["items"][0]
    old = (await client.get(f"/v1/exports/{snapshot['id']}")).json()
    assert old["snapshot"] == snapshot["snapshot"]


class FullCreditFake(FakeEvaluator):
    async def assess(self, request):
        return Evaluation(
            candidate_level_id=max(request.criterion.levels, key=lambda level: level.ordinal).id,
            requested_model="fake-v1",
            returned_model="fake-v1",
            adapter_version="fake-v1",
            usage={},
        )


async def test_csv_pending_score_is_empty_even_with_nonzero_suggestion(
    client, db, settings, scenario
):
    worker = Worker(db, settings, evaluator=FullCreditFake())
    while await worker.run_once():
        pass
    item_id = scenario["batch"]["item_ids"][0]
    await post(client, f"/v1/grading-items/{item_id}/reviews", review_payload())
    exported = await post(
        client, f"/v1/grading-batches/{scenario['batch']['id']}/exports", {"mode": "working"}
    )
    pending = next(row for row in exported["snapshot"]["rows"] if row["review_status"] == "pending")
    response = await client.get(f"/v1/exports/{exported['id']}", params={"format": "csv"})
    rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
    row = next(row for row in rows if row["answer_version_id"] == pending["answer"]["id"])
    assert row["review_status"] == "pending"
    assert row["suggested_score"] == "10.00" and row["confirmed_score"] == ""
    assert row["review_revision"] == "0"
    assert any(row["student_name"] == "'=unsafe-csv-name" for row in rows)

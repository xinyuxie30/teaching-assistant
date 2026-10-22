import copy
from uuid import UUID, uuid4

import httpx
import pytest
from conftest import post
from sqlalchemy import update
from teaching_assistant import analysis
from teaching_assistant.api import create_app
from teaching_assistant.cli import bootstrap
from teaching_assistant.db import GradingItem
from teaching_assistant.demo import DEMO_RUBRIC
from teaching_assistant.settings import Settings
from test_workflow import drain

pytestmark = pytest.mark.postgres


def review(*levels, revision=0, criteria=None):
    return {
        "expected_revision": revision,
        "decisions": [
            {
                "criterion_id": criterion["id"],
                "level_id": level,
                "evidence_support": "teacher_judgment",
                "reason": "Synthetic teacher decision.",
                "citations": [],
            }
            for criterion, level in zip(criteria or DEMO_RUBRIC["criteria"], levels, strict=True)
        ],
    }


async def read_analysis(client, batch):
    response = await client.get(f"/v1/grading-batches/{batch['id']}/analysis")
    assert response.status_code == 200, response.text
    return response.json()


async def test_analysis_empty_sample_and_unconfirmed_scores_are_not_zero(
    client, db, settings, scenario
):
    batch = scenario["batch"]
    before = await read_analysis(client, batch)
    assert before["coverage"] == {
        "assignment_roster": 2,
        "batch_answers": 2,
        "batch_students": 2,
        "confirmed": 0,
        "pending_review": 0,
        "failed": 0,
        "in_progress": 2,
        "simulated_confirmed": 0,
        "not_in_batch": 0,
        "confirmed_rate": "0.00",
    }
    assert before["scores"]["average_score"] is None
    assert all(
        point["average_points"] is None and point["score_rate"] is None
        for point in before["criteria"]
    )
    await drain(db, settings)
    pending = await read_analysis(client, batch)
    assert pending["coverage"]["pending_review"] == 2
    assert pending["coverage"]["in_progress"] == 0
    assert pending["scores"]["average_score"] is None
    assert sum(band["students"] for band in pending["scores"]["distribution"]) == 0
    # A real teacher-confirmed zero is part of the sample; the other suggestion is not.
    await post(
        client, f"/v1/grading-items/{batch['item_ids'][0]}/reviews", review("missing", "missing")
    )
    result = await read_analysis(client, batch)
    assert result["coverage"]["confirmed"] == result["coverage"]["pending_review"] == 1
    assert result["coverage"]["simulated_confirmed"] == 1
    assert result["scores"]["average_score"] == result["scores"]["minimum_score"] == "0.00"
    assert result["scores"]["distribution"][0]["students"] == 1
    assert all(point["below_full_credit_students"] == 1 for point in result["criteria"])


async def test_latest_reviews_exact_levels_and_stale_drilldown(client, db, settings, scenario):
    await drain(db, settings)
    batch = scenario["batch"]
    for item_id, levels in zip(
        batch["item_ids"], [("complete", "missing"), ("missing", "complete")], strict=True
    ):
        await post(client, f"/v1/grading-items/{item_id}/reviews", review(*levels))
    result = await read_analysis(client, batch)
    assert result["scores"]["average_score"] == "5.00"
    assert result["scores"]["minimum_score"] == "4.00"
    assert result["scores"]["maximum_score"] == "6.00"
    assert [band["students"] for band in result["scores"]["distribution"]] == [1, 1, 0, 0]
    assert [(point["average_points"], point["score_rate"]) for point in result["criteria"]] == [
        ("3.00", "50.00"),
        ("2.00", "50.00"),
    ]
    path = f"/v1/grading-batches/{batch['id']}/analysis/students"
    params = {
        "criterion_id": "fixed_rule",
        "below_full_credit": True,
        "basis_hash": result["basis_hash"],
    }
    detail = (await client.get(path, params=params)).json()
    assert detail["total"] == 1
    assert detail["items"][0]["id"] == batch["item_ids"][1]
    assert detail["items"][0]["level_id"] == "missing"
    assert detail["items"][0]["review_revision"] == 1
    assert detail["items"][0]["review_reason"] == "Synthetic teacher decision."
    await post(
        client,
        f"/v1/grading-items/{batch['item_ids'][1]}/reviews",
        review("complete", "complete", revision=1),
    )
    stale = await client.get(path, params=params)
    assert stale.status_code == 409 and stale.json()["code"] == "analysis_changed"
    refreshed = await read_analysis(client, batch)
    assert refreshed["basis_hash"] != result["basis_hash"]
    assert refreshed["scores"]["average_score"] == "8.00"
    assert refreshed["coverage"]["confirmed"] == 2  # not three review revisions
    assert refreshed["criteria"][0]["id"] == "traceability"
    assert refreshed["criteria"][1]["below_full_credit_students"] == 0


async def test_revised_answer_is_explicit_and_does_not_fall_back_to_old_review(
    client, db, settings, scenario
):
    await drain(db, settings)
    details = [
        (await client.get(f"/v1/grading-items/{item_id}")).json()
        for item_id in scenario["batch"]["item_ids"]
    ]
    first = next(
        item["id"]
        for item in details
        if item["answer"]["id"] == scenario["imported"]["answer_version_ids"][0]
    )
    await post(client, f"/v1/grading-items/{first}/reviews", review("complete", "complete"))
    # The second version is pending: filtering confirmed first would silently pick the older answer.
    imported = await post(
        client,
        f"/v1/assignments/{scenario['assignment']['id']}/imports",
        {
            "rows": [{**scenario["rows"][0], "source_revision": "v2", "answer_text": "A revision"}],
        },
    )
    await post(
        client,
        "/v1/grading-batches",
        {
            **scenario["batch_payload"],
            "answer_version_ids": [
                *scenario["imported"]["answer_version_ids"],
                *imported["answer_version_ids"],
            ],
        },
        expected=422,
    )
    batch = await post(
        client,
        "/v1/grading-batches",
        {
            **scenario["batch_payload"],
            "answer_version_ids": [
                imported["answer_version_ids"][0],
                scenario["imported"]["answer_version_ids"][1],
            ],
        },
    )
    result = await read_analysis(client, batch)
    assert result["coverage"]["batch_answers"] == 2
    assert result["coverage"]["batch_students"] == 2
    assert result["coverage"]["in_progress"] == 1
    assert result["coverage"]["pending_review"] == 1
    assert result["coverage"]["confirmed"] == 0
    assert result["scores"]["average_score"] is None
    path = f"/v1/grading-batches/{batch['id']}/analysis/students"
    selected = (await client.get(path)).json()
    assert selected["total"] == 2
    assert {row["answer_version_id"] for row in selected["items"]} == {
        imported["answer_version_ids"][0],
        scenario["imported"]["answer_version_ids"][1],
    }
    assert (await client.get(path, params={"criterion_id": "fixed_rule"})).json()["total"] == 0
    # Sharing items with another batch neither duplicates them nor changes that batch's scope.
    original = await read_analysis(client, scenario["batch"])
    assert original["coverage"]["confirmed"] == 1


async def test_failed_missing_scope_and_fixed_rubric_versions(client, db, settings, scenario):
    original = scenario["batch"]
    async with db.sessions.begin() as session:
        await session.execute(
            update(GradingItem)
            .where(GradingItem.id == UUID(original["item_ids"][1]))
            .values(status="failed", last_error="synthetic_failure")
        )
    await drain(db, settings)
    await post(
        client, f"/v1/grading-items/{original['item_ids'][0]}/reviews", review("partial", "missing")
    )
    result = await read_analysis(client, original)
    assert result["coverage"]["failed"] == 1 and result["scores"]["average_score"] == "3.00"
    subset = await post(
        client,
        "/v1/grading-batches",
        {
            **scenario["batch_payload"],
            "answer_version_ids": scenario["imported"]["answer_version_ids"][:1],
        },
    )
    assert (await read_analysis(client, subset))["coverage"]["not_in_batch"] == 1
    changed = copy.deepcopy(DEMO_RUBRIC)
    changed["criteria"][0]["levels"][1]["points"] = "1.00"
    draft = await post(
        client,
        f"/v1/question-versions/{scenario['question']['id']}/rubric-drafts",
        {"rubric": changed},
    )
    frozen = await post(
        client, f"/v1/rubric-drafts/{draft['id']}/confirm", {"expected_revision": 1}
    )
    new_batch = await post(
        client,
        "/v1/grading-batches",
        {
            "rubric_version_id": frozen["id"],
            "answer_version_ids": scenario["imported"]["answer_version_ids"][:1],
        },
    )
    new = await read_analysis(client, new_batch)
    assert new["coverage"]["confirmed"] == 0 and new["scores"]["average_score"] is None
    assert new["scope"]["rubric_version"] == 2 and result["scope"]["rubric_version"] == 1
    assert (await read_analysis(client, original))["scores"]["average_score"] == "3.00"


async def test_analysis_pagination_validation_and_teacher_isolation(client, db, scenario):
    batch_id = scenario["batch"]["id"]
    path = f"/v1/grading-batches/{batch_id}/analysis"
    listing = path + "/students"
    first = (await client.get(listing, params={"limit": 1})).json()
    second = (await client.get(listing, params={"limit": 1, "cursor": first["next_cursor"]})).json()
    assert first["total"] == second["total"] == 2
    assert first["items"][0]["id"] != second["items"][0]["id"]
    assert first["has_more"] and not second["has_more"]
    for params in (
        {"limit": 101},
        {"criterion_id": "unknown"},
        {"below_full_credit": True},
        {"status": "pending_review", "criterion_id": "fixed_rule"},
        {"cursor": first["next_cursor"], "status": "in_progress"},
        {"basis_hash": "invalid"},
    ):
        assert (await client.get(listing, params=params)).status_code == 422
    other_settings = Settings(dev_api_token=f"other-{uuid4().hex}")
    await bootstrap(db, other_settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(other_settings, db)),
        base_url="http://test",
        headers={"Authorization": f"Bearer {other_settings.dev_api_token}"},
    ) as other:
        assert (await other.get(path)).status_code == 404
        assert (await other.get(listing)).status_code == 404
    assert (await client.get(path, headers={"Authorization": "Bearer invalid"})).status_code == 401


async def test_zero_weight_criterion_and_decimal_score_rounding(client, db, settings, scenario):
    rubric = {
        "global_instructions": "Synthetic decimal boundary rubric",
        "criteria": [
            {
                "id": "decimal",
                "description": "Decimal scoring",
                "maximum_points": "10.00",
                "levels": [
                    {"id": "low", "ordinal": 0, "description": "Lower", "points": "5.99"},
                    {"id": "middle", "ordinal": 1, "description": "Middle", "points": "6.00"},
                    {"id": "high", "ordinal": 2, "description": "Higher", "points": "10.00"},
                ],
            },
            {
                "id": "unscored",
                "description": "Unscored feedback",
                "maximum_points": "0.00",
                "levels": [
                    {"id": "low", "ordinal": 0, "description": "Lower", "points": "0.00"},
                    {"id": "high", "ordinal": 1, "description": "Higher", "points": "0.00"},
                ],
            },
        ],
    }
    draft = await post(
        client,
        f"/v1/question-versions/{scenario['question']['id']}/rubric-drafts",
        {"rubric": rubric},
    )
    frozen = await post(
        client, f"/v1/rubric-drafts/{draft['id']}/confirm", {"expected_revision": 1}
    )
    batch = await post(
        client,
        "/v1/grading-batches",
        {**scenario["batch_payload"], "rubric_version_id": frozen["id"]},
    )
    await drain(db, settings)
    for item_id, level in zip(batch["item_ids"], ["low", "middle"], strict=True):
        await post(
            client,
            f"/v1/grading-items/{item_id}/reviews",
            review(level, "low", criteria=rubric["criteria"]),
        )
    result = await read_analysis(client, batch)
    assert result["scores"]["average_score"] == "6.00"  # 5.995, half up
    assert [band["students"] for band in result["scores"]["distribution"]] == [1, 1, 0, 0]
    zero = next(point for point in result["criteria"] if point["id"] == "unscored")
    assert zero["average_points"] == "0.00" and zero["score_rate"] is None
    assert zero["below_full_credit_students"] == 0


async def test_summary_uses_one_snapshot_during_concurrent_teacher_review(
    client, db, settings, scenario, monkeypatch
):
    await drain(db, settings)
    item_id = scenario["batch"]["item_ids"][0]
    await post(client, f"/v1/grading-items/{item_id}/reviews", review("missing", "missing"))
    before = await read_analysis(client, scenario["batch"])
    original_sources = analysis.sources
    updated = False

    async def change_after_sources(session, teacher, batch_id):
        nonlocal updated
        result = await original_sources(session, teacher, batch_id)
        if not updated:
            updated = True
            await post(
                client,
                f"/v1/grading-items/{item_id}/reviews",
                review("complete", "complete", revision=1),
            )
        return result

    monkeypatch.setattr(analysis, "sources", change_after_sources)
    during = await read_analysis(client, scenario["batch"])
    assert during["basis_hash"] == before["basis_hash"]
    assert during["scores"]["average_score"] == "0.00"
    assert all(point["average_points"] == "0.00" for point in during["criteria"])
    after = await read_analysis(client, scenario["batch"])
    assert after["basis_hash"] != during["basis_hash"]
    assert after["scores"]["average_score"] == "10.00"

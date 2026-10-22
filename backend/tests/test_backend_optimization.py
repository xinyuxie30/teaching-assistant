"""Bound database round trips while retaining scope, deduplication and snapshots."""

import asyncio
import importlib.util
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from conftest import post
from sqlalchemy import func, select
from teaching_assistant import service
from teaching_assistant.db import AnswerVersion, GradingItem
from teaching_assistant.domain import DomainError
from test_workflow import drain, review_payload

pytestmark = pytest.mark.postgres

_SPEC = importlib.util.spec_from_file_location(
    "benchmark_backend", Path(__file__).parents[1] / "tools" / "benchmark_backend.py"
)
benchmark = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(benchmark)


@pytest.mark.parametrize("size", [20, 200])
async def test_database_round_trips_do_not_grow_per_student(db, client, size):
    result = await benchmark.scenario(db, client, size)
    # Allow small fixed overhead changes, but reject per-answer database IO.
    for name, maximum in {
        "preview": 6,
        "import": 15,
        "create_batch": 20,
        "reuse_batch": 20,
        "export_pending": 25,
    }.items():
        assert result[name]["sql_statements"] <= maximum, (name, result[name])


async def test_bulk_import_keeps_composite_identity_order_and_atomic_conflicts(
    client, db, scenario
):
    base = scenario["rows"][0]
    rows = [
        {
            **base,
            "external_submission_id": "prefix:a",
            "source_revision": "b",
            "answer_text": "  😀原文\r\n",
        },
        {
            **base,
            "external_submission_id": "prefix",
            "source_revision": "a:b",
            "answer_text": "  另一份\n",
        },
        base,
    ]
    path = f"/v1/assignments/{scenario['assignment']['id']}/imports"
    imported = await post(client, path, {"rows": rows})
    assert imported["reused"] == 1
    assert len(set(imported["answer_version_ids"])) == 3
    repeated = await post(client, path, {"rows": list(reversed(rows))})
    assert repeated["answer_version_ids"] == list(reversed(imported["answer_version_ids"]))
    assert repeated["reused"] == 3
    for row, answer_id in zip(rows, imported["answer_version_ids"], strict=True):
        detail = (await client.get(f"/v1/answer-versions/{answer_id}")).json()
        assert detail["answer_text"] == row["answer_text"]
    await post(
        client,
        path,
        {
            "rows": [
                {**base, "source_revision": "would-be-new"},
                {**rows[0], "answer_text": "changed"},
            ]
        },
        expected=409,
    )
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AnswerVersion)) == 4


async def test_bulk_batches_with_distinct_keys_reuse_targets_in_consistent_lock_order(
    client, db, scenario
):
    original = scenario["batch_payload"]
    async with asyncio.timeout(10):
        batches = await asyncio.gather(
            *(
                post(
                    client,
                    "/v1/grading-batches",
                    {
                        **original,
                        "answer_version_ids": original["answer_version_ids"][
                            :: -1 if index % 2 else 1
                        ],
                    },
                )
                for index in range(4)
            )
        )
    assert len({batch["id"] for batch in batches}) == 4
    assert all(batch["item_ids"] == scenario["batch"]["item_ids"] for batch in batches)
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(GradingItem)) == 2


async def test_bulk_export_matches_full_item_details_including_reviews_and_receipts(
    db, settings, client, scenario
):
    await drain(db, settings)
    item_id = scenario["batch"]["item_ids"][0]
    await post(client, f"/v1/grading-items/{item_id}/reviews", review_payload())
    details = {
        value: (await client.get(f"/v1/grading-items/{value}")).json()
        for value in scenario["batch"]["item_ids"]
    }
    for mode in ("working", "confirmed_only"):
        exported = await post(
            client, f"/v1/grading-batches/{scenario['batch']['id']}/exports", {"mode": mode}
        )
        for row in exported["snapshot"]["rows"]:
            expected = details[row["id"]]
            assert {key: row[key] for key in expected} == expected
            assert row["student_name"] == expected["student"]["name"]
        assert len(exported["snapshot"]["rows"]) == (2 if mode == "working" else 1)


@pytest.mark.parametrize("mode", ["working", "confirmed_only"])
async def test_export_retains_one_snapshot_when_review_commits_between_bulk_reads(
    db, settings, client, scenario, monkeypatch, mode
):
    await drain(db, settings)
    entered, release = asyncio.Event(), asyncio.Event()
    original = service.item_views

    async def paused(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(service, "item_views", paused)
    operation = asyncio.create_task(
        post(client, f"/v1/grading-batches/{scenario['batch']['id']}/exports", {"mode": mode})
    )
    try:
        await asyncio.wait_for(entered.wait(), 5)
        item_id = scenario["batch"]["item_ids"][0]
        await post(client, f"/v1/grading-items/{item_id}/reviews", review_payload())
        release.set()
        exported = await asyncio.wait_for(operation, 5)
    finally:
        release.set()
        if not operation.done():
            operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)
    snapshot = exported["snapshot"]
    # FK validation against the concurrently updated batch can force PostgreSQL
    # to retry the whole transaction. Either complete snapshot is valid; mixing
    # pre-review counts with post-review detail is not.
    confirmed = snapshot["counts"]["confirmed"]
    assert confirmed in {0, 1}
    assert len(snapshot["rows"]) == (2 if mode == "working" else confirmed)
    assert sum(row["review_revision"] > 0 for row in snapshot["rows"]) == confirmed
    for row in snapshot["rows"]:
        assert row["review_revision"] == len(row["reviews"])
        assert (row["confirmed_score"] is not None) == (row["review_revision"] > 0)
    current = (await client.get(f"/v1/grading-items/{item_id}")).json()
    assert current["review_revision"] == 1


async def test_bulk_detail_rejects_mixed_owned_and_missing_ids(db, scenario):
    item_id = UUID(scenario["batch"]["item_ids"][0])
    async with db.sessions() as session:
        teacher = await session.scalar(
            select(GradingItem.teacher_id).where(GradingItem.id == item_id)
        )
        with pytest.raises(DomainError, match="Grading item not found"):
            await service.item_views(session, teacher, [item_id, uuid4()])

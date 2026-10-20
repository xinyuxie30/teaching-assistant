import asyncio
from uuid import UUID

import pytest
from conftest import post
from sqlalchemy import func, select, text, update
from teaching_assistant.db import Assessment, Attempt, GradingItem, ModelCall, Step
from teaching_assistant.evaluators import FakeEvaluator, FakeEvidenceExtractor
from teaching_assistant.settings import Settings
from teaching_assistant.worker import Worker

pytestmark = pytest.mark.postgres


def selected_item(scenario):
    return UUID(scenario["batch"]["item_ids"][0])


async def expire(db, item_id):
    async with db.sessions.begin() as session:
        await session.execute(
            update(GradingItem)
            .where(GradingItem.id == item_id)
            .values(lease_until=func.clock_timestamp() - text("interval '1 second'"))
        )


class CountingEvaluator(FakeEvaluator):
    def __init__(self):
        self.calls = 0

    async def assess(self, request):
        self.calls += 1
        return await super().assess(request)


class FailFirstExtractor(FakeEvidenceExtractor):
    def __init__(self):
        self.calls = 0

    async def extract(self, request):
        self.calls += 1
        if self.calls == 1:
            raise TimeoutError()
        return await super().extract(request)


async def test_retry_reuses_successful_evaluation_checkpoint(db, settings, scenario):
    item_id = selected_item(scenario)
    evaluator, extractor = CountingEvaluator(), FailFirstExtractor()
    worker = Worker(db, settings, evaluator, extractor, item_ids=(item_id,))
    assert await worker.run_once()
    async with db.sessions() as session:
        item = await session.get(GradingItem, item_id)
        assert item.status == "retry_wait" and item.suggested_score is None
        assert item.calls_used == 2
    restarted = Worker(db, settings, evaluator, extractor, item_ids=(item_id,))
    assert await restarted.run_once()
    assert evaluator.calls == 2  # Two criteria, even though extraction was retried.
    async with db.sessions() as session:
        item = await session.get(GradingItem, item_id)
        assert item.status == "succeeded" and item.calls_used == 5
        assert await session.scalar(select(func.count()).select_from(Step)) == 4


async def test_reclaimed_lease_resumes_saved_steps(db, settings, scenario):
    item_id = selected_item(scenario)
    evaluator = CountingEvaluator()
    old = Worker(db, settings, evaluator=evaluator, item_ids=(item_id,))
    lease = await old.claim()
    _, requests = await old.context(lease)
    await old.execute_step(lease, requests[0], "evaluate")
    # Simulate the process disappearing between persisted checkpoints.
    await expire(db, item_id)
    restarted = Worker(db, settings, evaluator=evaluator, item_ids=(item_id,))
    assert await restarted.run_once()
    assert evaluator.calls == 2
    async with db.sessions() as session:
        item = await session.get(GradingItem, item_id)
        assert item.status == "succeeded" and item.epoch == 2 and item.calls_used == 4
        attempts = list(await session.scalars(select(Attempt.status).order_by(Attempt.epoch)))
        assert attempts == ["abandoned", "succeeded"]


async def test_expired_worker_cannot_write_late_provider_result(db, scenario):
    item_id = selected_item(scenario)
    started, release = asyncio.Event(), asyncio.Event()

    class DelayedEvaluator(FakeEvaluator):
        async def assess(self, request):
            started.set()
            await release.wait()
            return await super().assess(request)

    settings = Settings(call_timeout_seconds=2, lease_seconds=5, retry_delay_seconds=0)
    old = Worker(db, settings, evaluator=DelayedEvaluator(), item_ids=(item_id,))
    running = asyncio.create_task(old.run_once())
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        await expire(db, item_id)
        newer = Worker(db, settings, item_ids=(item_id,))
        assert await newer.run_once()
        release.set()
        await running
    finally:
        release.set()
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
    async with db.sessions() as session:
        item = await session.get(GradingItem, item_id)
        assert item.status == "succeeded" and item.epoch == 2
        assert await session.scalar(select(func.count()).select_from(Assessment)) == 2
        assert await session.scalar(select(func.count()).select_from(Step)) == 4
        old_call = await session.scalar(select(ModelCall).where(ModelCall.epoch == 1))
        assert old_call.status == "unknown"


async def test_timeout_attempt_budget_and_explicit_retry(client, db, settings, scenario):
    class TimeoutEvaluator(FakeEvaluator):
        async def assess(self, request):
            await asyncio.sleep(30)
            return await super().assess(request)

    item_id = selected_item(scenario)
    worker = Worker(db, settings, evaluator=TimeoutEvaluator(), item_ids=(item_id,))
    for _ in range(3):
        assert await worker.run_once()
    assert not await worker.run_once()
    view = (await client.get(f"/v1/grading-items/{item_id}")).json()
    assert view["status"] == "failed"
    assert view["suggested_score"] is None and view["confirmed_score"] is None
    assert view["execution"]["calls_used"] == 3
    snapshot = await post(
        client,
        f"/v1/grading-batches/{scenario['batch']['id']}/exports",
        {
            "mode": "working",
        },
    )
    failed = next(r for r in snapshot["snapshot"]["rows"] if r["id"] == str(item_id))
    assert failed["suggested_score"] is None and failed["review_status"] == "pending"
    await post(
        client,
        f"/v1/grading-items/{item_id}/retry",
        {
            "reason": "Recovered provider; retry only this answer.",
            "additional_attempts": 1,
            "additional_calls": 0,
        },
    )
    assert await Worker(db, settings, item_ids=(item_id,)).run_once()
    view = (await client.get(f"/v1/grading-items/{item_id}")).json()
    assert view["status"] == "succeeded" and view["execution"]["attempts"] == 4
    assert view["execution"]["calls_used"] == 7  # The old calls remain charged to the budget.


async def test_invalid_evidence_fails_without_publishing_a_grade(db, settings, scenario):
    class FabricatingExtractor(FakeEvidenceExtractor):
        async def extract(self, request):
            return {
                "citations": [{"quote": "not in the answer", "start": 0, "end": 17}],
                "absence_claim": False,
                "extractor_version": "fault-injection",
            }

    item_id = selected_item(scenario)
    worker = Worker(db, settings, extractor=FabricatingExtractor(), item_ids=(item_id,))
    for _ in range(3):
        await worker.run_once()
    async with db.sessions() as session:
        item = await session.get(GradingItem, item_id)
        assert item.status == "failed" and item.suggested_score is None
        assert item.last_error == "invalid_evidence"
        assert await session.scalar(select(func.count()).select_from(Assessment)) == 0
        assert await session.scalar(select(func.count()).select_from(Step)) == 1

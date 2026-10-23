"""Contention and interruption at the boundaries between durable work phases."""

import asyncio
from datetime import timedelta
from uuid import UUID

import pytest
from conftest import post
from sqlalchemy import func, select
from teaching_assistant import worker as grading_worker
from teaching_assistant.db import (
    AgentArtifact,
    AgentEvent,
    AgentRun,
    AgentWake,
    GradingBatch,
    GradingItem,
)
from teaching_assistant.harness import catalog, waiting
from teaching_assistant.harness import worker as harness_worker
from teaching_assistant.harness.pool import run_pool
from teaching_assistant.harness.worker import HarnessWorker, LostAgentLease

pytestmark = pytest.mark.postgres


async def start_run(client, scenario):
    run = await post(
        client,
        f"/v1/courses/{scenario['workspace']['course_id']}/lesson-agent-runs",
        {"brief": {"topic": "Recovery", "objectives": ["Check recovery"], "duration_minutes": 10}},
    )
    return UUID(run["id"])


async def test_due_timers_skip_locked_roots_before_applying_limit(db, client, scenario):
    run_ids = [await start_run(client, scenario) for _ in range(3)]
    async with db.sessions() as session:
        now = await session.scalar(select(func.clock_timestamp()))
    for index, run_id in enumerate(run_ids):
        await post(
            client,
            f"/v1/agent-runs/{run_id}/wait",
            {
                "reason": "Timer contention",
                "wake_at": (now + timedelta(seconds=1, milliseconds=index)).isoformat(),
            },
        )
    await asyncio.sleep(1.05)
    async with db.sessions.begin() as locks:
        await locks.execute(
            select(AgentRun.id).where(AgentRun.id.in_(run_ids[:2])).with_for_update()
        )
        assert await waiting.scan_due_waits(db, limit=1) == 1
        async with db.sessions() as session:
            ready = await session.get(AgentRun, run_ids[2])
            assert ready.status == "queued" and ready.models_used == ready.tools_used == 0
    counts = await asyncio.gather(
        waiting.scan_due_waits(db, limit=1), waiting.scan_due_waits(db, limit=1)
    )
    assert sum(counts) == 2
    assert await waiting.scan_due_waits(db, limit=1) == 0
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AgentWake)) == 3


@pytest.mark.parametrize("interruption", ["cancel", "expired_lease", "replaced_owner"])
async def test_interruption_before_invoke_never_dispatches_a_provider(
    db, settings, client, scenario, monkeypatch, interruption
):
    run_id = await start_run(client, scenario)
    worker = HarnessWorker(db, settings, run_ids=(run_id,))
    lease = await worker.claim()
    request, proceed = await worker.prepare_step(lease)
    assert request is not None and proceed
    if interruption == "cancel":
        await post(client, f"/v1/agent-runs/{run_id}/cancel", {})
    else:
        async with db.sessions.begin() as session:
            run = await session.get(AgentRun, run_id)
            if interruption == "expired_lease":
                run.lease_until = await session.scalar(select(func.clock_timestamp())) - timedelta(
                    seconds=1
                )
            else:
                run.lease_owner = "replacement-worker"
    calls = []
    original = worker.build_model

    class ObservedModel:
        async def respond(self, body, policy):
            calls.append(body)
            return await original(policy).respond(body, policy)

    monkeypatch.setattr(worker, "build_model", lambda policy: ObservedModel())
    with pytest.raises(LostAgentLease):
        await worker.invoke(lease, *request)
    assert calls == []


async def test_context_work_cannot_dispatch_after_the_execution_deadline(
    db, settings, client, scenario, monkeypatch
):
    run_id = await start_run(client, scenario)
    worker = HarnessWorker(db, settings, run_ids=(run_id,))
    lease = await worker.claim()
    original = harness_worker.prepare_context

    async def expired_context(session, run, task):
        body = await original(session, run, task)
        # A delayed context builder reaches the deadline while holding the root lock.
        run.expires_at = await session.scalar(select(func.clock_timestamp())) - timedelta(seconds=1)
        return body

    monkeypatch.setattr(harness_worker, "prepare_context", expired_context)
    await worker.execute(lease)
    async with db.sessions() as session:
        run = await session.get(AgentRun, run_id)
        assert run.status == "failed" and run.last_error == "agent_deadline_exceeded"
        assert run.models_used == run.tree_models_used == 0 and run.inflight_seq is None
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AgentEvent)
                .where(AgentEvent.kind == "model_dispatched")
            )
            == 0
        )


async def test_tool_cannot_publish_an_artifact_after_the_execution_deadline(
    db, settings, client, scenario, monkeypatch
):
    run_id = await start_run(client, scenario)
    worker = HarnessWorker(db, settings, run_ids=(run_id,))
    lease = await worker.claim()
    for index in range(2):
        request, proceed = await worker.prepare_step(lease)
        assert proceed and request is not None
        assert await worker.save_reply(lease, await worker.invoke(lease, *request))
        if index == 0:
            assert await worker.prepare_step(lease) == (None, True)
    task = catalog.resolve("lesson-plan")
    original = task.apply_result
    entered = False

    async def slow_result(session, run, name, result):
        nonlocal entered
        await original(session, run, name, result)
        if name == "propose_lesson_plan":
            entered = True
            await asyncio.sleep(0.6)

    monkeypatch.setattr(task, "apply_result", slow_result)
    async with db.sessions.begin() as session:
        run = await session.get(AgentRun, run_id)
        run.expires_at = await session.scalar(select(func.clock_timestamp())) + timedelta(
            seconds=0.5
        )
    await worker.execute(lease)
    async with db.sessions() as session:
        run = await session.get(AgentRun, run_id)
        assert entered and run.status == "failed" and run.last_error == "agent_deadline_exceeded"
        assert not run.pending_calls
        assert await session.scalar(select(func.count()).select_from(AgentArtifact)) == 0


async def test_grading_claim_lease_starts_after_batch_lock_contention(
    db, settings, scenario, monkeypatch
):
    item_id = UUID(scenario["batch"]["item_ids"][0])
    worker = grading_worker.Worker(db, settings, item_ids=(item_id,))
    entered = asyncio.Event()
    original = grading_worker.refresh_batches

    async def refresh(session, selected):
        entered.set()
        await original(session, selected)

    monkeypatch.setattr(grading_worker, "refresh_batches", refresh)
    claim = None
    try:
        async with db.sessions.begin() as locks:
            await locks.execute(
                select(GradingBatch.id)
                .where(GradingBatch.id == UUID(scenario["batch"]["id"]))
                .with_for_update()
            )
            claim = asyncio.create_task(worker.claim())
            await asyncio.wait_for(entered.wait(), timeout=2)
            await asyncio.sleep(settings.lease_seconds + 0.1)
            assert not claim.done()
        lease = await asyncio.wait_for(claim, timeout=2)
        async with db.sessions() as session:
            item = await session.get(GradingItem, item_id)
            now = await session.scalar(select(func.clock_timestamp()))
            assert item.lease_until > now
        await worker.execute(lease)
        async with db.sessions() as session:
            item = await session.get(GradingItem, item_id)
            assert item.status == "succeeded" and item.attempts == 1
    finally:
        if claim is not None:
            claim.cancel()
            await asyncio.gather(claim, return_exceptions=True)


@pytest.mark.parametrize("interruption", ["expired_lease", "replaced_owner"])
async def test_inflight_call_stops_on_lease_loss_and_is_recovered_once(
    db, settings, client, scenario, monkeypatch, interruption
):
    settings.call_timeout_seconds = 2
    run_id = await start_run(client, scenario)
    worker = HarnessWorker(db, settings, run_ids=(run_id,))
    lease = await worker.claim()
    request, _ = await worker.prepare_step(lease)
    started, stopped, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class BlockedModel:
        async def respond(self, body, policy):
            started.set()
            try:
                await release.wait()
            finally:
                stopped.set()

    monkeypatch.setattr(worker, "build_model", lambda policy: BlockedModel())
    invocation = asyncio.create_task(worker.invoke(lease, *request))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        async with db.sessions.begin() as session:
            run = await session.get(AgentRun, run_id)
            if interruption == "expired_lease":
                run.lease_until = await session.scalar(select(func.clock_timestamp())) - timedelta(
                    seconds=1
                )
            else:
                run.lease_owner = "replacement-worker"
        with pytest.raises(LostAgentLease):
            await asyncio.wait_for(invocation, timeout=1.5)
        assert stopped.is_set()
    finally:
        invocation.cancel()
        await asyncio.gather(invocation, return_exceptions=True)
    async with db.sessions.begin() as session:
        run = await session.get(AgentRun, run_id)
        assert run.models_used == run.tree_models_used == 1 and run.inflight_seq is not None
        run.lease_until = await session.scalar(select(func.clock_timestamp())) - timedelta(
            seconds=1
        )
    assert await HarnessWorker(db, settings, run_ids=(run_id,)).run_once()
    async with db.sessions() as session:
        run = await session.get(AgentRun, run_id)
        assert run.status == "waiting_teacher" and run.attempts == 2
        assert run.models_used == run.tree_models_used == 3
        assert await session.scalar(select(func.count()).select_from(AgentArtifact)) == 1
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AgentEvent)
                .where(AgentEvent.kind == "model_unknown")
            )
            == 1
        )


async def test_competing_pools_claim_each_run_once(db, settings, client, scenario, monkeypatch):
    settings.call_timeout_seconds = 2
    settings.poll_seconds = 0.01
    run_ids = [await start_run(client, scenario) for _ in range(12)]
    original = harness_worker.build_model
    overlap, release = asyncio.Event(), asyncio.Event()
    active = peak = 0

    class ObservedModel:
        def __init__(self, delegate):
            self.delegate = delegate

        async def respond(self, body, policy):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active >= 4:
                overlap.set()
            try:
                await release.wait()
                return await self.delegate.respond(body, policy)
            finally:
                active -= 1

    monkeypatch.setattr(
        harness_worker,
        "build_model",
        lambda settings, policy: ObservedModel(original(settings, policy)),
    )
    pools = [
        asyncio.create_task(run_pool(db, settings, concurrency=4, mode="drain", run_ids=run_ids))
        for _ in range(2)
    ]
    try:
        await asyncio.wait_for(overlap.wait(), timeout=2)
        release.set()
        assert sum(await asyncio.wait_for(asyncio.gather(*pools), timeout=12)) == len(run_ids)
    finally:
        for task in pools:
            task.cancel()
        await asyncio.gather(*pools, return_exceptions=True)
    async with db.sessions() as session:
        runs = list(await session.scalars(select(AgentRun).where(AgentRun.id.in_(run_ids))))
        assert 4 <= peak <= 8 and active == 0
        assert len(runs) == 12
        assert all(run.status == "waiting_teacher" and run.attempts == 1 for run in runs)
        assert all(run.models_used == run.tools_used == 2 for run in runs)
        assert all(run.lease_owner is run.lease_until is run.inflight_seq is None for run in runs)
        assert await session.scalar(select(func.count()).select_from(AgentArtifact)) == len(run_ids)
        assert await session.scalar(
            select(func.count()).select_from(AgentEvent).where(AgentEvent.kind == "claimed")
        ) == len(run_ids)

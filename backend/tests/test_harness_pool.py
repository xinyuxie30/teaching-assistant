"""One process executes independent leases and drains dynamically created task trees."""

import asyncio
from datetime import timedelta
from uuid import UUID

import pytest
from conftest import post
from sqlalchemy import func, select
from teaching_assistant.db import AgentArtifact, AgentEvent, AgentRun
from teaching_assistant.harness import worker as worker_module
from teaching_assistant.harness.pool import run_pool
from teaching_assistant.harness.worker import HarnessWorker

pytestmark = pytest.mark.postgres

BRIEF = {
    "topic": "可复核的版本记录",
    "objectives": ["说明固定记录与可追溯来源的关系"],
    "duration_minutes": 10,
}


@pytest.fixture
async def pool_course(client, settings):
    # The app and worker share this fixture's Settings object; pin the same
    # timeout in profiles before creating tasks that wait on a model barrier.
    settings.call_timeout_seconds = 2
    settings.poll_seconds = 0.01
    workspace = await post(
        client,
        "/v1/workspaces",
        {
            "course_name": "Pool verification",
            "classroom_name": "Pool classroom",
            "students": [{"external_id": "pool-1", "name": "Fixture"}],
        },
    )
    return workspace["course_id"]


async def create_run(client, course, *, parallel=False):
    result = await post(
        client,
        f"/v1/courses/{course}/lesson-agent-runs",
        {
            "brief": BRIEF,
            "agent_profile": "fake-lesson-parallel-agent-v1"
            if parallel
            else "fake-lesson-agent-v1",
        },
    )
    return UUID(result["id"])


async def snapshot(db):
    async with db.sessions() as session:
        runs = list(await session.scalars(select(AgentRun).order_by(AgentRun.depth, AgentRun.id)))
        events = list(await session.scalars(select(AgentEvent)))
        artifacts = list(await session.scalars(select(AgentArtifact)))
        return runs, events, artifacts


def blocking_models(monkeypatch):
    """A barrier detects overlap instead of depending on elapsed-time speedups."""
    real_factory = worker_module.build_model
    both_started = asyncio.Event()
    release = asyncio.Event()
    counts = {"active": 0, "peak": 0, "entered": 0, "cancelled": 0}

    class BlockedModel:
        def __init__(self, delegate):
            self.delegate = delegate

        async def respond(self, body, policy):
            counts["entered"] += 1
            counts["active"] += 1
            counts["peak"] = max(counts["peak"], counts["active"])
            if counts["active"] == 2:
                both_started.set()
            try:
                await release.wait()
                return await self.delegate.respond(body, policy)
            except asyncio.CancelledError:
                counts["cancelled"] += 1
                raise
            finally:
                counts["active"] -= 1

    monkeypatch.setattr(
        worker_module,
        "build_model",
        lambda settings, policy: BlockedModel(real_factory(settings, policy)),
    )
    return real_factory, both_started, release, counts


async def test_two_pool_slots_overlap_model_calls_with_independent_owners(
    db, settings, client, pool_course, monkeypatch
):
    run_ids = [await create_run(client, pool_course) for _ in range(2)]
    _, both_started, release, counts = blocking_models(monkeypatch)
    pool = asyncio.create_task(run_pool(db, settings, concurrency=2, mode="drain", run_ids=run_ids))
    try:
        await asyncio.wait_for(both_started.wait(), timeout=2)
        runs, events, artifacts = await snapshot(db)
        assert all(run.status == "running" for run in runs)
        assert len({run.lease_owner for run in runs}) == 2
        assert all(run.models_used == run.tree_models_used == 1 for run in runs)
        assert sum(event.kind == "model_dispatched" for event in events) == 2
        assert not artifacts
        release.set()
        assert await asyncio.wait_for(pool, timeout=5) == 2
    finally:
        pool.cancel()
        await asyncio.gather(pool, return_exceptions=True)
    runs, _, artifacts = await snapshot(db)
    assert counts["peak"] == 2 and counts["active"] == 0
    assert len(artifacts) == 2 and all(run.status == "waiting_teacher" for run in runs)
    assert all(run.attempts == 1 for run in runs)


async def test_pool_drain_claims_dynamic_children_and_resumes_both_ancestors(
    db, settings, client, pool_course
):
    root_id = await create_run(client, pool_course, parallel=True)
    processed = await asyncio.wait_for(
        run_pool(db, settings, concurrency=2, mode="drain", run_ids=(root_id,)), timeout=8
    )
    runs, events, artifacts = await snapshot(db)
    assert processed == 6  # Root twice, designer twice, review once, leaf once.
    assert len(runs) == len(artifacts) == 4
    root = next(run for run in runs if run.id == root_id)
    assert root.status == "waiting_teacher" and root.children_spawned == 3
    assert sorted(run.depth for run in runs) == [0, 1, 1, 2]
    assert all(run.status == "completed" for run in runs if run.id != root_id)
    assert root.tree_models_used == sum(run.models_used for run in runs) == 10
    assert root.tree_tools_used == sum(run.tools_used for run in runs) == 11
    assert all(run.attempts == 1 and not run.pending_calls for run in runs)
    assert sum(event.kind == "child_received" for event in events) == 3


async def test_once_does_not_reuse_an_initially_empty_slot_for_new_children(
    db, settings, client, pool_course, monkeypatch
):
    root_id = await create_run(client, pool_course, parallel=True)
    initial_claims_done = asyncio.Event()
    original_claim = HarnessWorker.claim
    original_factory = worker_module.build_model
    claims = []

    async def observed_claim(worker):
        lease = await original_claim(worker)
        claims.append(lease)
        if len(claims) == 2:
            initial_claims_done.set()
        return lease

    class InitialModelBarrier:
        def __init__(self, model):
            self.model = model

        async def respond(self, body, policy):
            await initial_claims_done.wait()
            return await self.model.respond(body, policy)

    monkeypatch.setattr(HarnessWorker, "claim", observed_claim)
    monkeypatch.setattr(
        worker_module,
        "build_model",
        lambda settings, policy: InitialModelBarrier(original_factory(settings, policy)),
    )
    processed = await asyncio.wait_for(
        run_pool(db, settings, concurrency=2, mode="once", run_ids=(root_id,)), timeout=5
    )
    runs, _, artifacts = await snapshot(db)
    assert processed == 1 and len(claims) == 2 and claims[1] is None
    assert len(runs) == 3 and not artifacts
    assert next(run for run in runs if run.id == root_id).status == "waiting_children"
    assert all(run.status == "queued" and run.models_used == 0 for run in runs if run.id != root_id)


async def test_cancelled_pool_keeps_reservations_and_recovers_expired_leases(
    db, settings, client, pool_course, monkeypatch
):
    run_ids = [await create_run(client, pool_course) for _ in range(2)]
    real_factory, both_started, _, counts = blocking_models(monkeypatch)
    pool = asyncio.create_task(run_pool(db, settings, concurrency=2, mode="drain", run_ids=run_ids))
    try:
        await asyncio.wait_for(both_started.wait(), timeout=2)
    finally:
        pool.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pool, timeout=2)
    runs, events, artifacts = await snapshot(db)
    epochs = {run.id: run.epoch for run in runs}
    assert counts["active"] == 0 and counts["cancelled"] == 2
    assert all(run.status == "running" and run.inflight_seq is not None for run in runs)
    assert all(run.models_used == run.tree_models_used == 1 for run in runs)
    assert all(run.lease_owner is not None and run.lease_until is not None for run in runs)
    assert not artifacts and not any(event.kind == "model_unknown" for event in events)
    monkeypatch.setattr(worker_module, "build_model", real_factory)
    async with db.sessions.begin() as session:
        now = await session.scalar(select(func.clock_timestamp()))
        for run in await session.scalars(select(AgentRun)):
            run.lease_until = now - timedelta(seconds=1)
    assert (
        await asyncio.wait_for(
            run_pool(db, settings, concurrency=2, mode="drain", run_ids=run_ids), timeout=5
        )
        == 2
    )
    runs, events, artifacts = await snapshot(db)
    assert len(artifacts) == 2
    assert all(run.status == "waiting_teacher" and run.attempts == 2 for run in runs)
    assert all(run.epoch > epochs[run.id] for run in runs)
    assert all(run.models_used == run.tree_models_used == 3 for run in runs)
    assert sum(event.kind == "model_unknown" for event in events) == 2
    assert sum(event.kind == "model_dispatched" for event in events) == 6


async def test_once_processes_at_most_the_initial_slot_count(db, settings, client, pool_course):
    run_ids = [await create_run(client, pool_course) for _ in range(3)]
    assert (
        await asyncio.wait_for(
            run_pool(db, settings, concurrency=2, mode="once", run_ids=run_ids), timeout=5
        )
        == 2
    )
    runs, _, artifacts = await snapshot(db)
    assert len(artifacts) == 2
    assert sorted(run.status for run in runs) == ["queued", "waiting_teacher", "waiting_teacher"]
    unclaimed = next(run for run in runs if run.status == "queued")
    assert unclaimed.models_used == unclaimed.tools_used == unclaimed.attempts == 0

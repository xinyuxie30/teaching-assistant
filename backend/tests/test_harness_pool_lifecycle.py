"""Scheduling semantics independent of provider or database latency."""

import asyncio
from types import SimpleNamespace

import pytest
from teaching_assistant.harness import catalog, pool
from teaching_assistant.harness.worker import HarnessWorker


async def test_once_does_not_reclaim_empty_slots_between_completion_polls(monkeypatch):
    release = asyncio.Event()
    workers = []
    original_wait = asyncio.wait

    class Worker:
        def __init__(self, *args, **kwargs):
            self.slot = len(workers)
            self.claims = 0
            workers.append(self)

        async def claim(self):
            self.claims += 1
            return self.slot if self.slot == 0 or self.claims > 1 else None

        async def execute(self, lease):
            await release.wait()

    async def observe_poll(*args, **kwargs):
        result = await original_wait(*args, **kwargs)
        # Keep the first execution alive through a full scheduler poll. Newly
        # appearing work must not be claimed by the initially unused slot.
        release.set()
        return result

    monkeypatch.setattr(pool, "HarnessWorker", Worker)
    monkeypatch.setattr(pool.asyncio, "wait", observe_poll)
    assert (
        await pool.run_pool(None, SimpleNamespace(poll_seconds=0.001), concurrency=2, mode="once")
        == 1
    )
    assert [worker.claims for worker in workers] == [1, 1]


async def test_timeout_stops_provider_before_post_timeout_database_check(settings, monkeypatch):
    worker = HarnessWorker(None, settings)
    stopped = asyncio.Event()
    checks = 0

    async def guard(lease):
        nonlocal checks
        checks += 1
        if checks > 1:
            # The guard may itself block on a database outage. The outbound
            # request must already have been cancelled when that happens.
            assert stopped.is_set()
        return 1

    class Model:
        async def respond(self, body, policy):
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

    monkeypatch.setattr(worker, "invocation_remaining", guard)
    monkeypatch.setattr(worker, "build_model", lambda policy: Model())
    with pytest.raises(TimeoutError):
        await worker.invoke(None, {}, {}, 0.01)
    assert checks == 2 and stopped.is_set()


async def test_pool_builds_profiles_once_with_independent_worker_snapshots(settings, monkeypatch):
    workers = []
    builds = 0
    original = catalog.profiles

    def counted(*args, **kwargs):
        nonlocal builds
        builds += 1
        return original(*args, **kwargs)

    class IdleWorker(HarnessWorker):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            workers.append(self)

        async def claim(self):
            return None

    monkeypatch.setattr(catalog, "profiles", counted)
    monkeypatch.setattr(pool, "HarnessWorker", IdleWorker)
    assert await pool.run_pool(None, settings, concurrency=8, mode="drain") == 0
    assert builds == 1
    assert len({worker.owner for worker in workers}) == 8
    profile_id = next(iter(workers[0].available))
    workers[0].available[profile_id]["scheduler"]["child_max_model_calls"] = 999
    assert all(
        worker.available[profile_id]["scheduler"]["child_max_model_calls"] == 5
        for worker in workers[1:]
    )

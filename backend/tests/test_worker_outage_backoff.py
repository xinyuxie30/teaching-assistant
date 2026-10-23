"""Outages must not multiply reconnection attempts by the execution slot count."""

import asyncio
from types import SimpleNamespace

import pytest
from teaching_assistant.harness import pool
from teaching_assistant.harness.worker import HarnessWorker
from teaching_assistant.worker import Worker


async def test_pool_outage_attempts_one_claim_per_backoff(monkeypatch, caplog):
    claims = 0
    delays = []

    class DisconnectedWorker:
        def __init__(self, *args, **kwargs):
            pass

        async def claim(self):
            nonlocal claims
            claims += 1
            raise ConnectionError("fixture-sensitive-sql")

    async def sleep(delay):
        delays.append(delay)
        if len(delays) == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(pool, "HarnessWorker", DisconnectedWorker)
    monkeypatch.setattr(pool.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await pool.run_pool(None, SimpleNamespace(poll_seconds=0.1), concurrency=8)
    assert claims == 3
    assert delays == [0.25, 0.5, 1.0]
    assert "fixture-sensitive-sql" not in caplog.text


@pytest.mark.parametrize("worker_type", [Worker, HarnessWorker])
async def test_worker_outage_backoff_resets_after_progress(
    settings, monkeypatch, caplog, worker_type
):
    settings.poll_seconds = 0.1
    worker = worker_type(None, settings)
    outcomes = iter([ConnectionError, ConnectionError, True, ConnectionError, False])
    delays = []

    async def once():
        outcome = next(outcomes, None)
        if outcome is None:
            raise asyncio.CancelledError
        if outcome is ConnectionError:
            raise ConnectionError("fixture-sensitive-sql")
        return outcome

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(worker, "run_once", once)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await worker.run_forever()
    assert delays == [0.25, 0.5, 0.25, 0.1]
    assert "fixture-sensitive-sql" not in caplog.text

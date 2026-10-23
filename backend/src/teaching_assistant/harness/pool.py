"""Bounded, work-conserving execution slots using the existing durable leases."""

import asyncio
import logging

from teaching_assistant.harness.worker import HarnessWorker
from teaching_assistant.worker_backoff import failure_delay


async def run_pool(db, settings, *, concurrency=1, mode="forever", run_ids=None):
    if type(concurrency) is not int or not 1 <= concurrency <= 16:
        raise ValueError("Harness concurrency must be an integer between 1 and 16")
    if mode not in {"forever", "drain", "once"}:
        raise ValueError("Unknown Harness pool mode")
    free, available = [], None
    for _ in range(concurrency):
        worker = HarnessWorker(db, settings, run_ids=run_ids, available_profiles=available)
        free.append(worker)
        # Profile generation is CPU work; doing it for every slot blocks initial
        # claims and in-flight siblings. Each worker still owns a detached copy.
        if available is None:
            available = getattr(worker, "available", None)
    running = {}
    processed = 0
    failures = 0
    try:
        while True:
            failed = False
            for worker in list(free):
                try:
                    lease = await worker.claim()
                except Exception as exc:
                    if mode != "forever":
                        raise
                    logging.getLogger(__name__).error(
                        "agent_pool_claim_error: %s", type(exc).__name__
                    )
                    # All slots use the same database. Do not multiply a failed
                    # connection attempt by the number of free execution slots.
                    failed = True
                    break
                if lease is not None:
                    running[asyncio.create_task(worker.execute(lease))] = worker
                    free.remove(worker)
            if not running:
                if mode != "forever":
                    return processed
                failures = failures + 1 if failed else 0
                await asyncio.sleep(
                    failure_delay(settings.poll_seconds, failures)
                    if failed
                    else settings.poll_seconds
                )
                continue
            # A completed slot can claim descendants while another model is still
            # responding. No AsyncSession or lease owner is shared between slots.
            done, _ = await asyncio.wait(
                running, timeout=settings.poll_seconds, return_when=asyncio.FIRST_COMPLETED
            )
            for execution in done:
                worker = running.pop(execution)
                try:
                    await execution
                except Exception as exc:
                    if mode != "forever":
                        raise
                    logging.getLogger(__name__).error(
                        "agent_pool_execute_error: %s", type(exc).__name__
                    )
                    failed = True
                processed += 1
                if mode != "once":
                    free.append(worker)
            if mode == "once":
                # Each slot gets one initial claim. Unused slots cannot turn a
                # one-shot invocation into a drain as new child work appears.
                free.clear()
                if not running:
                    return processed
            failures = failures + 1 if failed else 0
            if failed:
                await asyncio.sleep(failure_delay(settings.poll_seconds, failures))
    finally:
        # Shutdown does not create more claims. Dispatched calls keep their usage
        # reservation; the next process recovers their expired, fenced leases.
        for execution in running:
            execution.cancel()
        await asyncio.gather(*running, return_exceptions=True)

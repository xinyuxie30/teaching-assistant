"""Candidate paging must progress beyond locks and sleeping parents."""

from datetime import timedelta
from uuid import UUID

import pytest
from conftest import post
from sqlalchemy import func, select, update
from teaching_assistant.db import AgentRun
from teaching_assistant.harness import scheduler
from teaching_assistant.harness.worker import HarnessWorker

pytestmark = pytest.mark.postgres


async def test_claim_passes_multiple_locked_pages_with_equal_ready_times(
    db, settings, client, scenario
):
    run_ids = [
        UUID(
            (
                await post(
                    client,
                    f"/v1/question-versions/{scenario['question']['id']}/rubric-agent-runs",
                    {},
                )
            )["id"]
        )
        for _ in range(5)
    ]
    async with db.sessions.begin() as session:
        await session.execute(
            update(AgentRun).where(AgentRun.id.in_(run_ids)).values(ready_at=func.now())
        )
    ordered = sorted(run_ids)
    worker = HarnessWorker(db, settings, run_ids=tuple(run_ids))
    worker.candidate_page_size = 2
    async with db.sessions.begin() as locks:
        await locks.execute(
            select(AgentRun.id).where(AgentRun.id.in_(ordered[:4])).with_for_update()
        )
        lease = await worker.claim()
        assert lease is not None and lease.run_id == ordered[-1]
    await worker.execute(lease)
    async with db.sessions() as session:
        rows = list(await session.scalars(select(AgentRun).where(AgentRun.id.in_(run_ids))))
        assert sum(row.status == "waiting_teacher" for row in rows) == 1
        assert sum(row.status == "queued" for row in rows) == 4


async def test_one_row_page_does_not_hide_child_behind_waiting_parent(
    db, settings, client, scenario
):
    root = await post(
        client,
        f"/v1/courses/{scenario['workspace']['course_id']}/lesson-agent-runs",
        {
            "agent_profile": "fake-lesson-team-agent-v1",
            "brief": {
                "topic": "Versioning",
                "objectives": ["Explain history"],
                "duration_minutes": 10,
            },
        },
    )
    worker = HarnessWorker(db, settings, run_ids=(UUID(root["id"]),))
    worker.candidate_page_size = 1
    assert await worker.run_once()
    async with db.sessions() as session:
        child = await session.scalar(
            select(AgentRun).where(AgentRun.parent_run_id == UUID(root["id"]))
        )
        child_id = child.id
    lease = await worker.claim()
    assert lease is not None and lease.run_id == child_id
    await worker.execute(lease)
    assert await worker.run_once()
    assert not await worker.run_once()
    detail = (await client.get(f"/v1/agent-runs/{root['id']}")).json()
    assert detail["status"] == "waiting_teacher"


@pytest.mark.parametrize(
    "profile",
    [
        "fake-lesson-team-agent-v1",
        "fake-lesson-parallel-agent-v1",
        "fake-background-lesson-agent-v1",
    ],
)
async def test_claim_does_not_lock_sleeping_parent_before_available_child(
    db, settings, client, scenario, monkeypatch, profile
):
    created = await post(
        client,
        f"/v1/courses/{scenario['workspace']['course_id']}/lesson-agent-runs",
        {
            "agent_profile": profile,
            "brief": {"topic": "Recovery", "objectives": ["Preserve work"], "duration_minutes": 10},
            "max_model_calls": 12,
        },
    )
    root_id = UUID(created["id"])
    worker = HarnessWorker(db, settings, run_ids=(root_id,))
    assert await worker.run_once()
    locked = []
    original = scheduler.lock_run

    async def observed_lock(session, run_id, **options):
        locked.append(run_id)
        return await original(session, run_id, **options)

    monkeypatch.setattr(scheduler, "lock_run", observed_lock)
    lease = await worker.claim()
    assert lease is not None and lease.run_id != root_id
    assert root_id not in locked
    async with db.sessions.begin() as session:
        # Deadline cleanup must bypass the sleeping-parent filter.
        root = await session.get(AgentRun, root_id)
        root.expires_at = await session.scalar(select(func.clock_timestamp())) - timedelta(
            seconds=1
        )
    assert not await worker.claim()
    async with db.sessions() as session:
        root = await session.get(AgentRun, root_id)
        assert root.status == "failed" and root.last_error == "agent_deadline_exceeded"

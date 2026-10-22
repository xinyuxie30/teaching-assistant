"""Resume compatibility and atomic teacher-answer acceptance at the worker boundary."""

import asyncio
import json
from uuid import UUID

import pytest
from conftest import post
from sqlalchemy import select
from teaching_assistant.db import AgentArtifact, AgentRun
from teaching_assistant.domain import DomainError, digest
from teaching_assistant.harness import catalog
from teaching_assistant.harness.pool import run_pool
from teaching_assistant.harness.worker import HarnessWorker
from test_harness_parallel import BRIEF, rows, start
from test_harness_parallel import parallel_course as parallel_course  # noqa: F401

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("failure", ["reducer", "timeout", "hook"])
async def test_answer_acceptance_failure_rolls_back_state_and_pairs_once(
    db, settings, client, parallel_course, monkeypatch, failure
):
    created = await post(
        client,
        f"/v1/courses/{parallel_course}/lesson-agent-runs",
        {"brief": BRIEF, "agent_profile": "fake-assisted-lesson-agent-v1"},
    )
    run_id = UUID(created["id"])
    if failure == "hook":
        # Runtime hook overrides are a legacy extension point; v5 pins registered hooks.
        async with db.sessions.begin() as session:
            run = await session.get(AgentRun, run_id)
            run.manifest = catalog.v4_manifest(
                settings, "lesson-plan-assisted", "fake-assisted-lesson-agent-v1"
            )
            run.profile_id = "agent-" + digest(run.manifest)
    assert await HarnessWorker(db, settings, run_ids=(run_id,)).run_once()
    waiting = (await client.get(f"/v1/agent-runs/{run_id}")).json()["wait"]
    await post(
        client,
        f"/v1/agent-runs/{run_id}/wake",
        {"wait_token": waiting["token"], "response": "保留完整教学要求"},
    )
    worker = HarnessWorker(db, settings, run_ids=(run_id,))
    lease = await worker.claim()
    assert lease is not None
    task = catalog.resolve("lesson-plan-assisted")

    async def failing_reducer(session, run, name, result):
        run.task_state = {**run.task_state, "partial_answer": result}
        await session.flush()
        if failure == "timeout":
            await asyncio.sleep(3)
        raise DomainError("answer_rejected", "Reject this answer")

    class MutatingHook:
        def post_tool(self, name, result):
            result["response"] = "A hook must never rewrite the teacher's answer"

    if failure == "hook":
        worker.override_hooks = MutatingHook()
    else:
        monkeypatch.setattr(task, "apply_result", failing_reducer)
    assert await worker.prepare_step(lease) == (None, True)
    async with db.sessions() as session:
        run = await session.get(AgentRun, run_id)
        assert run.status == "running" and run.pending_calls == []
        assert "partial_answer" not in run.task_state and "teacher_answer" not in run.task_state
        assert run.tools_used == run.tree_tools_used == 3
        assert run.models_used == run.tree_models_used == 3
        results = [
            m for m in run.messages if m.get("tool_call_id") == waiting["question"]["tool_call_id"]
        ]
        assert len(results) == 1
        expected = {
            "reducer": "answer_rejected",
            "timeout": "tool_timeout",
            "hook": "invalid_tool_result",
        }[failure]
        assert json.loads(results[0]["content"]) == {"ok": False, "error_code": expected}
        assert await session.scalar(select(AgentArtifact.id)) is None


async def test_v3_parallel_tree_resumes_with_its_original_policies(
    db, settings, client, parallel_course
):
    created = await start(client, parallel_course)
    run_id = UUID(created["id"])
    async with db.sessions.begin() as session:
        run = await session.get(AgentRun, run_id)
        run.manifest = catalog.v3_manifest(
            settings, "lesson-plan-parallel", "fake-lesson-parallel-agent-v1"
        )
        run.profile_id = "agent-" + digest(run.manifest)
    await run_pool(db, settings, concurrency=2, mode="drain", run_ids=(run_id,))
    tree = await rows(db, run_id)
    assert len(tree) == 4
    assert tree[0].status == "waiting_teacher"
    assert all(run.status == "completed" for run in tree[1:])
    for run in tree:
        assert run.manifest["runtime_version"] == "harness-runtime-v3"
        assert run.manifest["context_version"] == "semantic-assistant-history-v1"
        assert "interactive_version" not in run.manifest
        assert "memory_version" not in run.manifest
    assert tree[0].tree_models_used == 10 and tree[0].tree_tools_used == 11

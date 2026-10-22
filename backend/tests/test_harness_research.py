"""Large result references, semantic checkpoints and resumed real worker protocols."""

import json
from copy import deepcopy
from uuid import UUID

import pytest
from conftest import post
from sqlalchemy import func, select
from teaching_assistant.db import AgentArtifact, AgentContextCheckpoint, AgentRun, AgentToolResult
from teaching_assistant.domain import digest
from teaching_assistant.harness import catalog, tool_results
from teaching_assistant.harness.context import build_payload
from teaching_assistant.harness.context_store import accept_summary, prepare_context
from teaching_assistant.harness.semantic_compaction import SummaryRequest, fake_summary_reply
from teaching_assistant.harness.worker import HarnessWorker
from test_harness_assisted_memory import BRIEF, create_course, knowledge

pytestmark = pytest.mark.postgres


async def start(client):
    course = await create_course(client)
    memories = [
        await knowledge(
            client, course, title=f"课程经验{i}", body=(f"经验{i}：先独立思考再讨论。" * 90)
        )
        for i in range(5)
    ]
    run = await post(
        client,
        f"/v1/courses/{course}/lesson-agent-runs",
        {
            "agent_profile": "fake-research-lesson-agent-v1",
            "brief": BRIEF,
            "max_total_tokens": 1000000,
            "max_cost_usd": "0.10",
        },
    )
    return UUID(run["id"]), memories


async def test_large_reference_pages_survive_question_restart_without_truncating_history(
    db, settings, client
):
    run_id, memories = await start(client)
    assert await HarnessWorker(db, settings, run_ids=(run_id,)).run_once()
    waiting = (await client.get(f"/v1/agent-runs/{run_id}")).json()
    assert waiting["status"] == "waiting_input", waiting
    async with db.sessions() as session:
        run = await session.get(AgentRun, run_id)
        saved = await session.scalar(
            select(AgentToolResult).where(AgentToolResult.run_id == run_id)
        )
        checkpoints = list(
            await session.scalars(
                select(AgentContextCheckpoint).where(AgentContextCheckpoint.run_id == run_id)
            )
        )
        assert saved.body["items"] == list(reversed(memories))
        assert len(saved.body["items"][0]["body"]) > 1000
        assert any(
            m.get("role") == "tool" and memories[0]["body"] in m["content"] for m in run.messages
        )
        assert checkpoints and "source_view" in checkpoints[-1].details
        original_reference = saved.id
    await post(
        client,
        f"/v1/agent-runs/{run_id}/wake",
        {"wait_token": waiting["wait"]["token"], "response": "  小组活动后独立书写。  "},
    )
    assert await HarnessWorker(db, settings, run_ids=(run_id,)).run_once()
    done = (await client.get(f"/v1/agent-runs/{run_id}")).json()
    assert done["status"] == "waiting_teacher", done
    async with db.sessions() as session:
        artifact = await session.scalar(select(AgentArtifact).where(AgentArtifact.run_id == run_id))
        assert {item["id"] for item in artifact.body["memory_references"]} == {
            m["id"] for m in memories
        }
        assert artifact.body["teacher_response"] == "  小组活动后独立书写。  "
        assert await session.scalar(select(func.count()).select_from(AgentToolResult)) == 1
        assert await session.get(AgentToolResult, original_reference) is not None


async def test_reference_source_and_semantic_checkpoint_restore_together(db, settings, client):
    run_id, _ = await start(client)
    worker = HarnessWorker(db, settings, run_ids=(run_id,))
    lease = await worker.claim()
    # Execute only the two actual read tools, leaving an ordinary completed transcript.
    for _ in range(4):
        request, ongoing = await worker.prepare_step(lease)
        assert ongoing
        if request is not None:
            reply = await worker.invoke(lease, *request)
            assert await worker.save_reply(lease, reply)
    async with db.sessions.begin() as session:
        run = await session.get(AgentRun, run_id)
        assert "memory_snapshot" in run.task_state and not run.pending_calls
        run.messages = [
            *run.messages[:2],
            {
                "role": "assistant",
                "content": "Tentative historical plan, not verified facts. " * 120,
            },
            *run.messages[2:],
            *({"role": "assistant", "content": f"Recent note {i}"} for i in range(8)),
        ]
        source = deepcopy(run.messages)
        task = catalog.resolve(run.task_type)
        view = await tool_results.project_messages(session, run)
        no_long_note = [m for i, m in enumerate(view.messages) if i != 2]
        base = len(
            json.dumps(
                build_payload(no_long_note, run.manifest, task.registry.schemas()),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        run.limits = {**run.limits, "max_context_characters": base + 1600}
        request = await prepare_context(session, run, task)
        assert isinstance(request, SummaryRequest)
        await accept_summary(
            session, run, task, request.plan, fake_summary_reply(request.body, run.manifest)
        )
    async with db.sessions.begin() as session:
        run = await session.get(AgentRun, run_id)
        prepared = await prepare_context(session, run, catalog.resolve(run.task_type))
        assert isinstance(prepared, dict)
        assert run.messages == source
        saved = await session.scalar(
            select(AgentContextCheckpoint)
            .where(AgentContextCheckpoint.run_id == run_id)
            .order_by(AgentContextCheckpoint.sequence.desc())
        )
        assert saved.details["summaries"]
        assert saved.details["source_view"]["original_source_hash"] == digest(source)
        assert any(
            "externalized" in m.get("content", "")
            for m in prepared["messages"]
            if isinstance(m.get("content"), str)
        )

"""Real summary acceptance and crash recovery over explicitly synthetic history."""

import json
from copy import deepcopy
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select
from teaching_assistant.db import AgentContextCheckpoint, AgentEvent, AgentRun
from teaching_assistant.domain import ModelError
from teaching_assistant.harness import catalog
from teaching_assistant.harness.context import build_payload, tool_result
from teaching_assistant.harness.context_store import prepare_context
from teaching_assistant.harness.lesson import NoArguments, read_lesson_brief
from teaching_assistant.harness.semantic_compaction import SummaryRequest, is_summary_request
from teaching_assistant.harness.store import event


async def run_summary_case(client, db, settings, worker_class, course_id, max_cost_usd):
    response = await client.post(
        f"/v1/courses/{course_id}/lesson-agent-runs",
        headers={"Idempotency-Key": uuid4().hex},
        json={
            "agent_profile": "deepseek-lesson-agent-v1",
            "instructions": "依据固定简报设计10分钟的小练习。已有历史笔记只是待核实计划，不能替代简报；保存未确认方案后停止。",
            "brief": {
                "topic": "固定版本与来源",
                "objectives": ["说明为何记录版本和来源"],
                "duration_minutes": 10,
            },
            "max_model_calls": 6,
            "max_tool_calls": 8,
            "max_context_characters": 30000,
            "max_total_tokens": 100000,
            "max_cost_usd": str(max_cost_usd),
            "max_seconds": 240,
        },
    )
    response.raise_for_status()
    run_id = UUID(response.json()["id"])
    async with db.sessions.begin() as session:
        run = await session.get(AgentRun, run_id)
        task = catalog.resolve(run.task_type)
        facts = await read_lesson_brief(session, run, NoArguments())
        await task.apply_result(session, run, "read_lesson_brief", facts)
        call = {
            "id": "synthetic-history-brief",
            "type": "function",
            "function": {"name": "read_lesson_brief", "arguments": "{}"},
        }
        run.messages = [
            *run.messages,
            {
                "role": "assistant",
                "content": (
                    "Synthetic tentative planning: distinguish fixed versions from editable drafts. Preserve provenance; the teacher reviews the final proposal. This is a proposed approach, not verified classroom evidence. "
                    * 22
                ),
            },
            {"role": "assistant", "content": None, "tool_calls": [call]},
            tool_result(call["id"], {"ok": True, "result": facts}),
            *(
                {
                    "role": "assistant",
                    "content": f"Synthetic recent planning note {i}: keep the fixed objectives.",
                }
                for i in range(8)
            ),
        ]
        run.tools_used = run.tree_tools_used = 1
        event(
            session,
            run,
            "verification_fixture",
            description="Synthetic history and a code-read brief; no historical paid model calls are asserted.",
        )
        original = deepcopy(run.messages)
        size = len(
            json.dumps(
                build_payload(run.messages, run.manifest, task.registry.schemas()),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        # Leave space for a useful summary; the smallest possible threshold can
        # admit only the planner's one-character lower bound, not a real reply.
        for limit in range(min(size - 1000, 30000), 2999, -250):
            run.limits = {**run.limits, "max_context_characters": limit}
            try:
                prepared = await prepare_context(session, run, task)
            except ModelError:
                continue
            if isinstance(prepared, SummaryRequest):
                break
        else:
            raise AssertionError("Could not form a bounded synthetic summary fixture")
    worker = worker_class(db, settings, run_ids=(run_id,))
    lease = await worker.claim()
    request, ongoing = await worker.prepare_step(lease)
    assert ongoing and request is not None and is_summary_request(request[0])
    reply = await worker.invoke(lease, *request)
    assert await worker.save_reply(lease, reply)
    async with db.sessions.begin() as session:
        run = await session.get(AgentRun, run_id)
        assert run.messages == original
        checkpoint = await session.scalar(
            select(AgentContextCheckpoint).where(AgentContextCheckpoint.run_id == run_id)
        )
        assert checkpoint is not None and checkpoint.details.get("summaries")
        # Simulate a process ending after the committed receipt, then recover the
        # expired lease with the same pinned policy. No second summary is needed.
        run.lease_until = await session.scalar(select(func.clock_timestamp())) - timedelta(
            seconds=1
        )
    assert await worker_class(db, settings, run_ids=(run_id,)).run_once()
    async with db.sessions() as session:
        run = await session.get(AgentRun, run_id)
        assert run.status == "waiting_teacher", (run.status, run.last_error)
        events = list(
            await session.scalars(
                select(AgentEvent).where(
                    AgentEvent.run_id == run_id, AgentEvent.kind == "model_dispatched"
                )
            )
        )
        assert sum(e.body.get("purpose") == "context_summary" for e in events) == 1
    return run_id

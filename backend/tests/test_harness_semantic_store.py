"""Fenced, charged, durable summary model calls in isolated PostgreSQL schemas."""

from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch
from uuid import UUID

import pytest
from conftest import post
from sqlalchemy import func, select
from teaching_assistant.db import AgentContextCheckpoint, AgentEvent, AgentRun
from teaching_assistant.domain import digest
from teaching_assistant.harness import catalog
from teaching_assistant.harness.context import tool_result
from teaching_assistant.harness.context_store import accept_summary, prepare_context
from teaching_assistant.harness.rubric import NoArguments, read_question
from teaching_assistant.harness.semantic_compaction import (
    SEMANTIC_VERSION,
    SummaryRequest,
    fake_summary_reply,
    is_summary_request,
)
from teaching_assistant.harness.worker import HarnessWorker

pytestmark = pytest.mark.postgres


async def seed(client, scenario, db, settings, *, max_model_calls=6):
    policy = catalog.v3_manifest(settings, "rubric", "fake-rubric-agent-v1")
    old_profile = {
        "id": "agent-" + digest(policy),
        "alias": policy["alias"],
        "manifest": policy,
        "ready": True,
    }
    with patch("teaching_assistant.agent.service.profiles", return_value=[old_profile]):
        created = await post(
            client,
            f"/v1/question-versions/{scenario['question']['id']}/rubric-agent-runs",
            {"max_context_characters": 12000, "max_model_calls": max_model_calls},
        )
    run_id = UUID(created["id"])
    async with db.sessions.begin() as session:
        run = await session.get(AgentRun, run_id)
        assert run.manifest["context_version"] == SEMANTIC_VERSION
        facts = await read_question(session, run, NoArguments())
        run.messages = [
            *run.messages,
            {"role": "assistant", "content": "Historical tentative planning. " * 330},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "facts",
                        "type": "function",
                        "function": {"name": "read_question", "arguments": "{}"},
                    }
                ],
            },
            tool_result("facts", {"ok": True, "result": facts}),
            *(
                {"role": "assistant", "content": f"Recent tentative note {index}"}
                for index in range(8)
            ),
        ]
        run.question_read = True
        run.models_used = run.tools_used = run.tree_models_used = run.tree_tools_used = 1
        source = deepcopy(run.messages)
    return run_id, source


async def expire(db, run_id):
    async with db.sessions.begin() as session:
        run = await session.get(AgentRun, run_id)
        run.lease_until = await session.scalar(select(func.clock_timestamp())) - timedelta(
            seconds=1
        )


async def snapshot(db, run_id):
    async with db.sessions() as session:
        run = await session.get(AgentRun, run_id)
        checkpoints = list(
            await session.scalars(
                select(AgentContextCheckpoint).where(AgentContextCheckpoint.run_id == run_id)
            )
        )
        events = list(
            await session.scalars(
                select(AgentEvent).where(AgentEvent.run_id == run_id).order_by(AgentEvent.sequence)
            )
        )
        return run, checkpoints, events


async def test_summary_dispatch_charges_shared_budget_and_keeps_source_transcript(
    db, settings, client, scenario
):
    run_id, source = await seed(client, scenario, db, settings)
    worker = HarnessWorker(db, settings, run_ids=(run_id,))
    lease = await worker.claim()
    request, proceed = await worker.prepare_step(lease)
    assert proceed and is_summary_request(request[0])
    run, checkpoints, events = await snapshot(db, run_id)
    assert run.models_used == run.tree_models_used == 2
    assert not checkpoints and run.messages == source
    assert events[-1].body["purpose"] == "context_summary"
    assert events[-1].body["summary_plan"]["sources"][0]["source_index"] == 2
    reply = await worker.invoke(lease, *request)
    assert await worker.save_reply(lease, reply)
    run, checkpoints, events = await snapshot(db, run_id)
    assert run.messages == source and len(checkpoints) == 1
    assert checkpoints[0].details["version"] == SEMANTIC_VERSION
    assert run.inflight_seq is None
    assert events[-1].kind == "context_compacted"
    await worker.execute(lease)
    run, checkpoints, events = await snapshot(db, run_id)
    assert run.status == "waiting_teacher" and run.draft_id
    assert run.models_used == run.tree_models_used == 3
    assert run.messages[: len(source)] == source
    assert len(checkpoints) == 1
    assert (
        sum(e.kind == "model_dispatched" and e.body["purpose"] == "context_summary" for e in events)
        == 1
    )


async def test_committed_summary_survives_worker_restart_without_another_summary_call(
    db, settings, client, scenario
):
    run_id, source = await seed(client, scenario, db, settings)
    worker = HarnessWorker(db, settings, run_ids=(run_id,))
    lease = await worker.claim()
    request, _ = await worker.prepare_step(lease)
    await worker.save_reply(lease, await worker.invoke(lease, *request))
    await expire(db, run_id)
    assert await HarnessWorker(db, settings, run_ids=(run_id,)).run_once()
    run, checkpoints, events = await snapshot(db, run_id)
    assert run.status == "waiting_teacher" and run.attempts == 2
    assert run.models_used == run.tree_models_used == 3
    assert run.messages[: len(source)] == source
    assert len(checkpoints) == 1
    assert (
        sum(e.kind == "model_dispatched" and e.body["purpose"] == "context_summary" for e in events)
        == 1
    )


async def test_lost_summary_response_keeps_unknown_cost_reserved_on_recovery(
    db, settings, client, scenario
):
    run_id, source = await seed(client, scenario, db, settings)
    worker = HarnessWorker(db, settings, run_ids=(run_id,))
    lease = await worker.claim()
    request, _ = await worker.prepare_step(lease)
    assert is_summary_request(request[0])
    await expire(db, run_id)
    assert await HarnessWorker(db, settings, run_ids=(run_id,)).run_once()
    run, checkpoints, events = await snapshot(db, run_id)
    assert run.status == "waiting_teacher" and run.models_used == run.tree_models_used == 4
    assert run.messages[: len(source)] == source and len(checkpoints) == 1
    assert sum(e.kind == "model_unknown" for e in events) == 1
    assert (
        sum(e.kind == "model_dispatched" and e.body["purpose"] == "context_summary" for e in events)
        == 2
    )


async def test_summary_cannot_bypass_root_model_budget(db, settings, client, scenario):
    run_id, source = await seed(client, scenario, db, settings, max_model_calls=2)
    assert await HarnessWorker(db, settings, run_ids=(run_id,)).run_once()
    run, checkpoints, _ = await snapshot(db, run_id)
    assert run.status == "failed" and run.last_error == "agent_model_budget_exhausted"
    assert run.models_used == run.tree_models_used == 2
    assert run.messages == source and run.draft_id is None and len(checkpoints) == 1


async def test_summary_checkpoint_and_event_roll_back_with_receipt_transaction(
    db, settings, client, scenario
):
    run_id, source = await seed(client, scenario, db, settings)
    with pytest.raises(RuntimeError, match="rollback"):
        async with db.sessions.begin() as session:
            run = await session.get(AgentRun, run_id)
            task = catalog.resolve(run.task_type)
            request = await prepare_context(session, run, task)
            assert isinstance(request, SummaryRequest)
            await accept_summary(
                session, run, task, request.plan, fake_summary_reply(request.body, run.manifest)
            )
            await session.flush()
            raise RuntimeError("rollback")
    run, checkpoints, events = await snapshot(db, run_id)
    assert run.messages == source and not checkpoints
    assert not any(e.kind == "context_compacted" for e in events)


async def test_invalid_model_summary_fails_once_without_receipt_or_history_mutation(
    db, settings, client, scenario, monkeypatch
):
    run_id, source = await seed(client, scenario, db, settings)

    class InvalidSummaryModel:
        async def respond(self, body, policy):
            assert is_summary_request(body)
            reply = fake_summary_reply(body, policy).model_copy(update={"content": "not JSON"})
            reply.metadata.usage = {"input_tokens": 40, "output_tokens": 3}
            reply.metadata.provider_request_id = "invalid-summary-response"
            return reply

    monkeypatch.setattr(
        "teaching_assistant.harness.worker.build_model",
        lambda settings, policy: InvalidSummaryModel(),
    )
    assert await HarnessWorker(db, settings, run_ids=(run_id,)).run_once()
    run, checkpoints, events = await snapshot(db, run_id)
    assert run.status == "failed" and run.last_error == "agent_context_summary_invalid"
    assert run.models_used == 2 and run.messages == source and not checkpoints
    assert sum(e.kind == "model_failed" for e in events) == 1
    failure = next(e for e in events if e.kind == "model_failed")
    assert failure.body["usage"] == {"input_tokens": 40, "output_tokens": 3}
    assert failure.body["provider_request_id"] == "invalid-summary-response"
    assert not any(e.kind == "model_succeeded" for e in events)

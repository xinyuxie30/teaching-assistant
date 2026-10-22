"""Real worker transcripts, checkpoint recovery, and known rejected-response usage."""

import json
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from conftest import post
from sqlalchemy import func, select
from teaching_assistant.agent.context import initial_messages
from teaching_assistant.db import (
    AgentContextCheckpoint,
    AgentEvent,
    AgentRun,
    Course,
    QuestionVersion,
    Teacher,
)
from teaching_assistant.domain import DomainError, ModelMetadata, digest
from teaching_assistant.harness import catalog
from teaching_assistant.harness.context import build_payload, tool_result
from teaching_assistant.harness.contracts import Reply, ToolCall
from teaching_assistant.harness.rubric_model import FakeAgentModel
from teaching_assistant.harness.semantic_compaction import (
    SEMANTIC_TOOL_VERSION,
    fake_summary_reply,
    is_summary_request,
)
from teaching_assistant.harness.worker import HarnessWorker

pytestmark = pytest.mark.postgres


def read_reply(index, policy):
    return Reply(
        content="Tentative historical reasoning. " * 160 if index == 0 else f"Read {index}",
        tool_calls=[
            ToolCall.model_validate(
                {
                    "id": f"normal-read-{index}",
                    "function": {"name": "read_question", "arguments": "{" + " " * index + "}"},
                }
            )
        ],
        metadata=ModelMetadata(
            requested_model=policy["model"],
            returned_model=policy["model"],
            adapter_version="fixture",
        ),
    )


async def events_for(db, run_id):
    async with db.sessions() as session:
        row = await session.get(AgentRun, run_id)
        events = list(
            await session.scalars(
                select(AgentEvent).where(AgentEvent.run_id == run_id).order_by(AgentEvent.sequence)
            )
        )
        checkpoints = list(
            await session.scalars(
                select(AgentContextCheckpoint).where(AgentContextCheckpoint.run_id == run_id)
            )
        )
        return row, events, checkpoints


async def normal_loop_at_summary(db, settings, client, scenario, monkeypatch):
    policy = catalog.manifest(settings, "rubric", "fake-rubric-agent-v1")
    assert policy["context_version"] == SEMANTIC_TOOL_VERSION
    instructions = "Create a reviewable draft after checking the fixed facts."
    async with db.sessions() as session:
        question = await session.get(QuestionVersion, UUID(scenario["question"]["id"]))
        facts = {
            "question": question.prompt,
            "reference": question.reference,
            "maximum_points": str(question.maximum_points),
            "content_hash": question.content_hash,
            "version": question.version,
        }
    expected = initial_messages(instructions)
    for index in range(5):
        expected.extend(
            [
                read_reply(index, policy).message(),
                tool_result(f"normal-read-{index}", {"ok": True, "result": facts}),
            ]
        )
    body = build_payload(expected, policy, catalog.resolve("rubric").registry.schemas())
    limit = len(json.dumps(body, ensure_ascii=False, separators=(",", ":"))) - 100
    created = await post(
        client,
        f"/v1/question-versions/{scenario['question']['id']}/rubric-agent-runs",
        {"instructions": instructions, "max_context_characters": limit, "max_model_calls": 12},
    )
    run_id = UUID(created["id"])

    class NarratingModel:
        async def respond(self, body, policy):
            if is_summary_request(body):
                return fake_summary_reply(body, policy)
            count = sum(message["role"] == "tool" for message in body["messages"])
            if count < 5:
                return read_reply(count, policy)
            return await FakeAgentModel().respond(body, policy)

    monkeypatch.setattr(
        "teaching_assistant.harness.worker.build_model", lambda *_: NarratingModel()
    )
    worker = HarnessWorker(db, settings, run_ids=(run_id,))
    lease = await worker.claim()
    for _ in range(5):
        request, proceed = await worker.prepare_step(lease)
        assert proceed and not is_summary_request(request[0])
        assert await worker.save_reply(lease, await worker.invoke(lease, *request))
        request, proceed = await worker.prepare_step(lease)
        assert request is None and proceed
    row, _, checkpoints = await events_for(db, run_id)
    assert row.messages == expected and row.models_used == row.tools_used == 5
    assert not checkpoints
    request, proceed = await worker.prepare_step(lease)
    assert proceed and is_summary_request(request[0])
    return worker, lease, request, expected


async def test_normal_tool_loop_summarizes_narration_and_recovers_without_repaying(
    db, settings, client, scenario, monkeypatch
):
    worker, lease, request, original = await normal_loop_at_summary(
        db, settings, client, scenario, monkeypatch
    )
    assert await worker.save_reply(lease, await worker.invoke(lease, *request))
    row, _, checkpoints = await events_for(db, lease.run_id)
    assert row.messages == original and row.models_used == 6
    assert len(checkpoints) == 1
    projection = checkpoints[0].messages
    assert projection[2]["tool_calls"] == original[2]["tool_calls"]
    assert projection[:2] == original[:2] and projection[3:] == original[3:]
    assert "Untrusted historical assistant summary" in projection[2]["content"]
    async with db.sessions.begin() as session:
        run = await session.get(AgentRun, lease.run_id)
        run.lease_until = await session.scalar(select(func.clock_timestamp())) - timedelta(
            seconds=1
        )
    assert await HarnessWorker(db, settings, run_ids=(lease.run_id,)).run_once()
    row, events, checkpoints = await events_for(db, lease.run_id)
    assert row.status == "waiting_teacher" and row.draft_id
    assert row.models_used == row.tree_models_used == 7 and row.attempts == 2
    assert row.messages[: len(original)] == original
    assert len(checkpoints) == 1
    assert (
        sum(
            event.kind == "model_dispatched" and event.body["purpose"] == "context_summary"
            for event in events
        )
        == 1
    )


async def revoke_course(db, course_id):
    async with db.sessions.begin() as session:
        teacher = Teacher(name="Other test owner", token_digest=digest(uuid4().hex))
        session.add(teacher)
        await session.flush()
        course = await session.get(Course, UUID(course_id))
        course.teacher_id = teacher.id


def known_metadata(policy):
    return ModelMetadata(
        requested_model=policy["model"],
        returned_model=policy["model"],
        adapter_version="fixture-known-response",
        usage={"input_tokens": 83, "output_tokens": 7},
        provider_request_id="received-before-rejection",
        provider_fingerprint="known-fingerprint",
        http_status=200,
    )


@pytest.mark.parametrize("rejection", ["authorization", "artifact_validation"])
async def test_known_response_metadata_survives_business_rejection(
    db, settings, client, scenario, monkeypatch, rejection
):
    created = await post(
        client, f"/v1/question-versions/{scenario['question']['id']}/rubric-agent-runs", {}
    )
    run_id = UUID(created["id"])

    class RejectedModel:
        async def respond(self, body, policy):
            if rejection == "authorization":
                await revoke_course(db, scenario["workspace"]["course_id"])
            return Reply(content="Proposed completion", metadata=known_metadata(policy))

    async def rejected_artifact(*_):
        raise DomainError("fixture_artifact_rejected", "Invalid persisted artifact")

    if rejection == "artifact_validation":
        monkeypatch.setattr(catalog.resolve("rubric"), "artifact_exists", rejected_artifact)
    monkeypatch.setattr("teaching_assistant.harness.worker.build_model", lambda *_: RejectedModel())
    assert await HarnessWorker(db, settings, run_ids=(run_id,)).run_once()
    row, events, _ = await events_for(db, run_id)
    assert row.status == "failed" and row.draft_id is None and row.models_used == 1
    assert row.last_error == (
        "not_found" if rejection == "authorization" else "fixture_artifact_rejected"
    )
    failures = [event for event in events if event.kind == "model_failed"]
    assert len(failures) == 1
    assert failures[0].body["usage"] == {"input_tokens": 83, "output_tokens": 7}
    assert failures[0].body["provider_request_id"] == "received-before-rejection"
    assert failures[0].body["provider_fingerprint"] == "known-fingerprint"
    assert failures[0].body["http_status"] == 200
    assert not any(event.kind == "model_succeeded" for event in events)


async def test_received_summary_keeps_metadata_after_course_access_revoked(
    db, settings, client, scenario, monkeypatch
):
    worker, lease, request, original = await normal_loop_at_summary(
        db, settings, client, scenario, monkeypatch
    )
    reply = await worker.invoke(lease, *request)
    reply.metadata = known_metadata(request[1])
    await revoke_course(db, scenario["workspace"]["course_id"])
    from teaching_assistant.domain import ModelError

    with pytest.raises(ModelError) as failure:
        await worker.save_reply(lease, reply)
    await worker.fail(lease, failure.value)
    row, events, checkpoints = await events_for(db, lease.run_id)
    assert row.status == "failed" and row.last_error == "not_found"
    assert row.messages == original and not checkpoints
    rejected = next(event for event in events if event.kind == "model_failed")
    assert rejected.body["usage"] == {"input_tokens": 83, "output_tokens": 7}
    assert rejected.body["provider_request_id"] == "received-before-rejection"

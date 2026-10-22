"""Background children keep parents productive and join without paid polling."""

import json
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from conftest import post
from pydantic import ValidationError
from sqlalchemy import func, select
from teaching_assistant.db import AgentArtifact, AgentEvent, AgentRun
from teaching_assistant.domain import DomainError, digest
from teaching_assistant.harness import background, scheduler
from teaching_assistant.harness.background_lesson import FakeBackgroundLessonModel
from teaching_assistant.harness.contracts import Reply
from teaching_assistant.harness.worker import HarnessWorker


@pytest.mark.parametrize(
    "fields",
    [
        {"status": "success", "run_status": "completed"},
        {"status": "failed", "run_status": "failed"},
        {"status": "not_ready", "run_status": "running", "error_code": "should_not_fail"},
        {"status": "success", "run_status": "running", "error_code": "cannot_succeed"},
        {"status": "not_ready", "run_status": "completed"},
        {"status": "failed", "run_status": "running", "error_code": "cannot_be_failed"},
    ],
)
def test_child_output_schema_rejects_inconsistent_terminal_claims(fields):
    with pytest.raises(ValidationError):
        background.ChildOutput(child_run_id=uuid4(), task_type="lesson-review", **fields)


@pytest.fixture
async def background_course(client):
    result = await post(
        client,
        "/v1/workspaces",
        {
            "course_name": "Background course",
            "classroom_name": "Background class",
            "students": [{"external_id": "background-student", "name": "Student"}],
        },
    )
    return result["course_id"]


async def start(client, course, **limits):
    created = await post(
        client,
        f"/v1/courses/{course}/lesson-agent-runs",
        {
            "agent_profile": "fake-background-lesson-agent-v1",
            "brief": {
                "topic": "后台工作与明确等待",
                "objectives": ["说明后台子任务与主任务的边界"],
                "duration_minutes": 10,
            },
            "max_model_calls": 10,
            "max_tool_calls": 12,
            **limits,
        },
    )
    return UUID(created["id"])


async def waiting_root(db, settings, client, course, **limits):
    root_id = await start(client, course, **limits)
    assert await HarnessWorker(db, settings, run_ids=(root_id,)).run_once()
    async with db.sessions() as session:
        root = await session.get(AgentRun, root_id)
        child = await session.scalar(select(AgentRun).where(AgentRun.parent_run_id == root_id))
        assert root.status == "waiting_children"
        assert child is not None
        return root_id, child.id


def receipts(run, tool):
    calls = {
        call["id"]
        for message in run.messages
        if message["role"] == "assistant"
        for call in message.get("tool_calls", [])
        if call["function"]["name"] == tool
    }
    return [
        json.loads(message["content"])
        for message in run.messages
        if message.get("tool_call_id") in calls
    ]


@pytest.mark.postgres
async def test_parent_works_while_child_is_queued_and_joins_once_after_restart(
    db, settings, client, background_course
):
    root_id, child_id = await waiting_root(db, settings, client, background_course)
    async with db.sessions() as session:
        root = await session.get(AgentRun, root_id)
        child = await session.get(AgentRun, child_id)
        assert child.status == "queued" and child.models_used == 0
        assert root.task_state["brief_read"] is True
        assert (
            root.models_used
            == root.tools_used
            == root.tree_models_used
            == root.tree_tools_used
            == 4
        )
        assert receipts(root, "spawn_lesson_review")[0]["result"]["status"] == "scheduled"
        assert receipts(root, "get_child_output")[0]["result"]["status"] == "not_ready"
        assert len(root.pending_calls) == 1
        assert root.pending_calls[0]["function"]["name"] == "join_child"
        assert receipts(root, "join_child") == []
    # A different worker/process completes the child, then another receives the join.
    assert await HarnessWorker(db, settings, run_ids=(child_id,)).run_once()
    assert await HarnessWorker(db, settings, run_ids=(root_id,)).run_once()
    async with db.sessions() as session:
        root = await session.get(AgentRun, root_id)
        child = await session.get(AgentRun, child_id)
        assert root.status == "waiting_teacher" and child.status == "completed"
        assert root.attempts == 1
        assert root.tree_models_used == root.tree_tools_used == 7
        assert root.models_used == root.tools_used == 5
        output = receipts(root, "join_child")
        assert len(output) == 1 and output[0]["result"]["status"] == "success"
        artifact = await session.scalar(
            select(AgentArtifact).where(AgentArtifact.run_id == root_id)
        )
        child_artifact = await session.scalar(
            select(AgentArtifact).where(AgentArtifact.run_id == child_id)
        )
        assert artifact.body["review"] == child_artifact.body
        assert artifact.body["review_reference"] == {
            "child_run_id": str(child_id),
            "artifact_id": str(child_artifact.id),
            "content_hash": child_artifact.content_hash,
        }
        assert artifact.confirmed_at is child_artifact.confirmed_at is None
        assert len(root.messages) == 12  # Initial pair plus five model/tool pairs.


@pytest.mark.postgres
async def test_join_wait_does_not_poll_a_model_or_recharge_tools(
    db, settings, client, background_course
):
    root_id, child_id = await waiting_root(db, settings, client, background_course)
    child_worker = HarnessWorker(db, settings, run_ids=(child_id,))
    child_lease = await child_worker.claim()
    assert child_lease is not None
    for _ in range(2):
        assert not await HarnessWorker(db, settings, run_ids=(root_id,)).run_once()
    async with db.sessions.begin() as session:
        _, root = await scheduler.lock_run(session, root_id)
        assert background.is_join_wait(root)
        assert await background.pending_join(session, root) is None
        assert root.tree_models_used == root.tree_tools_used == 4
        dispatches = await session.scalar(
            select(func.count())
            .select_from(AgentEvent)
            .where(AgentEvent.run_id == root_id, AgentEvent.kind == "model_dispatched")
        )
        assert dispatches == 4
    await child_worker.execute(child_lease)
    assert await HarnessWorker(db, settings, run_ids=(root_id,)).run_once()


@pytest.mark.postgres
async def test_already_finished_child_joins_without_suspending_parent(
    db, settings, client, background_course
):
    root_id = await start(client, background_course)
    worker = HarnessWorker(db, settings, run_ids=(root_id,))
    lease = await worker.claim()
    request, proceed = await worker.prepare_step(lease)
    assert proceed and request is not None
    assert await worker.save_reply(lease, await worker.invoke(lease, *request))
    _, proceed = await worker.prepare_step(lease)
    assert proceed
    async with db.sessions() as session:
        child = await session.scalar(select(AgentRun).where(AgentRun.parent_run_id == root_id))
        child_id = child.id
        assert (await session.get(AgentRun, root_id)).status == "running"
    assert await HarnessWorker(db, settings, run_ids=(child_id,)).run_once()
    await worker.execute(lease)
    async with db.sessions() as session:
        root = await session.get(AgentRun, root_id)
        assert root.status == "waiting_teacher"
        assert receipts(root, "get_child_output")[0]["result"]["status"] == "success"
        assert len(receipts(root, "join_child")) == 1
        assert not root.task_state.get("background_joins")


@pytest.mark.postgres
async def test_other_root_cannot_query_or_join_the_child(db, settings, client, background_course):
    root_id, child_id = await waiting_root(db, settings, client, background_course)
    other_root_id = await start(client, background_course)
    async with db.sessions.begin() as session:
        _, other = await scheduler.lock_run(session, other_root_id)
        with pytest.raises(DomainError) as failure:
            await background.get_child_output(
                session, other, background.ChildReferenceInput(child_run_id=child_id)
            )
        assert failure.value.code == "agent_child_not_found"
        assert failure.value.status == 404
    async with db.sessions() as session:
        root = await session.get(AgentRun, root_id)
        assert root.status == "waiting_children"
        assert root.tree_models_used == 4


@pytest.mark.postgres
@pytest.mark.parametrize("problem", ["missing", "hash", "confirmed", "objectives"])
async def test_completed_child_requires_a_valid_unconfirmed_artifact(
    db, settings, client, background_course, problem
):
    root_id, child_id = await waiting_root(db, settings, client, background_course)
    async with db.sessions.begin() as session:
        _, child = await scheduler.lock_run(session, child_id)
        child.status = "completed"
        if problem != "missing":
            body = {
                "objectives": child.task_input["brief"]["objectives"],
                "checks": ["核对目标覆盖"],
            }
            if problem == "objectives":
                body["objectives"] = ["不属于固定简报的目标"]
            session.add(
                AgentArtifact(
                    run_id=child.id,
                    kind=child.task_type,
                    body=body,
                    content_hash="0" * 64 if problem == "hash" else digest(body),
                    confirmed_at=await session.scalar(select(func.clock_timestamp()))
                    if problem == "confirmed"
                    else None,
                )
            )
    async with db.sessions.begin() as session:
        _, root = await scheduler.lock_run(session, root_id)
        with pytest.raises(DomainError) as failure:
            await background.pending_join(session, root)
        assert failure.value.code == "invalid_child_result"
        assert receipts(root, "join_child") == []


@pytest.mark.postgres
async def test_root_cancel_closes_the_join_and_fences_a_claimed_background_child(
    db, settings, client, background_course
):
    root_id, child_id = await waiting_root(db, settings, client, background_course)
    child_worker = HarnessWorker(db, settings, run_ids=(child_id,))
    lease = await child_worker.claim()
    await post(client, f"/v1/agent-runs/{root_id}/cancel", {})
    await child_worker.execute(lease)
    async with db.sessions() as session:
        root = await session.get(AgentRun, root_id)
        child = await session.get(AgentRun, child_id)
        assert root.status == child.status == "cancelled"
        assert root.pending_calls == []
        assert len(receipts(root, "join_child")) == 1
        assert receipts(root, "join_child")[0]["ok"] is False
        assert await session.scalar(select(func.count()).select_from(AgentArtifact)) == 0


@pytest.mark.postgres
async def test_cancelled_child_delivers_terminal_failure_to_the_original_join(
    db, settings, client, background_course
):
    root_id, child_id = await waiting_root(db, settings, client, background_course)
    await post(client, f"/v1/agent-runs/{child_id}/cancel", {})
    assert await HarnessWorker(db, settings, run_ids=(root_id,)).run_once()
    async with db.sessions() as session:
        root = await session.get(AgentRun, root_id)
        result = receipts(root, "join_child")
        assert len(result) == 1
        assert result[0]["result"]["status"] == "failed"
        assert result[0]["result"]["run_status"] == "cancelled"
        assert root.status == "failed"


@pytest.mark.postgres
async def test_background_child_spends_the_same_root_budget(
    db, settings, client, background_course
):
    root_id, child_id = await waiting_root(
        db, settings, client, background_course, max_model_calls=5
    )
    assert await HarnessWorker(db, settings, run_ids=(child_id,)).run_once()
    async with db.sessions() as session:
        root = await session.get(AgentRun, root_id)
        child = await session.get(AgentRun, child_id)
        assert root.status == "failed" and root.last_error == "agent_model_budget_exhausted"
        assert child.status == "cancelled"
        assert root.tree_models_used == 5


@pytest.mark.postgres
async def test_join_receipt_survives_a_crash_before_resumed_parent_model_call(
    db, settings, client, background_course
):
    root_id, child_id = await waiting_root(db, settings, client, background_course)
    assert await HarnessWorker(db, settings, run_ids=(child_id,)).run_once()
    crashed = HarnessWorker(db, settings, run_ids=(root_id,))
    lease = await crashed.claim()
    assert lease is not None
    async with db.sessions.begin() as session:
        _, root = await scheduler.lock_run(session, root_id)
        assert len(receipts(root, "join_child")) == 1
        root.lease_until = await session.scalar(select(func.clock_timestamp())) - timedelta(
            seconds=1
        )
    assert await HarnessWorker(db, settings, run_ids=(root_id,)).run_once()
    async with db.sessions() as session:
        root = await session.get(AgentRun, root_id)
        assert root.status == "waiting_teacher"
        assert root.tree_models_used == root.tree_tools_used == 7
        assert len(receipts(root, "join_child")) == 1


@pytest.mark.postgres
async def test_bad_child_artifact_produces_one_join_error_instead_of_stuck_claim(
    db, settings, client, background_course
):
    root_id, child_id = await waiting_root(db, settings, client, background_course)
    async with db.sessions.begin() as session:
        root, child = await scheduler.lock_run(session, child_id)
        original_join_id = root.pending_calls[0]["id"]
        child.status = "completed"  # Deliberately missing the required immutable artifact.
    worker = HarnessWorker(db, settings, run_ids=(root_id,))
    lease = await worker.claim()
    assert lease is not None
    async with db.sessions() as session:
        root = await session.get(AgentRun, root_id)
        delivered = [
            message for message in root.messages if message.get("tool_call_id") == original_join_id
        ]
        assert len(delivered) == 1
        assert json.loads(delivered[0]["content"]) == {
            "ok": False,
            "error_code": "invalid_child_result",
        }
        assert root.status == "running"
        assert root.tree_models_used == root.tree_tools_used == 4
    await worker.execute(lease)
    async with db.sessions() as session:
        root = await session.get(AgentRun, root_id)
        assert root.status == "failed"
        assert (
            sum(message.get("tool_call_id") == original_join_id for message in root.messages) == 1
        )
        assert root.tree_models_used <= root.limits["max_model_calls"]


@pytest.mark.postgres
@pytest.mark.parametrize("failure", ["duplicate_call", "premature_stop"])
async def test_parent_protocol_failure_immediately_cancels_its_background_child(
    db, settings, client, background_course, monkeypatch, failure
):
    original = FakeBackgroundLessonModel.respond

    async def failed_parent(self, body, policy):
        ordinary = await original(self, body, policy)
        spawned = [
            call
            for message in body["messages"]
            if message["role"] == "assistant"
            for call in message.get("tool_calls", [])
            if call["function"]["name"] == "spawn_lesson_review"
        ]
        if not spawned:
            return ordinary
        if failure == "duplicate_call":
            return Reply.model_validate({"tool_calls": [spawned[0]], "metadata": ordinary.metadata})
        return Reply(content="Completed without creating an artifact.", metadata=ordinary.metadata)

    monkeypatch.setattr(FakeBackgroundLessonModel, "respond", failed_parent)
    root_id = await start(client, background_course)
    assert await HarnessWorker(db, settings, run_ids=(root_id,)).run_once()
    async with db.sessions() as session:
        root = await session.get(AgentRun, root_id)
        child = await session.scalar(select(AgentRun).where(AgentRun.parent_run_id == root_id))
        assert root.status == "failed"
        assert child.status == "cancelled"
        assert child.models_used == 0
        assert child.lease_owner is None and child.lease_until is None

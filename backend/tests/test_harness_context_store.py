"""Real PostgreSQL recovery and atomicity for durable context projections."""

from copy import deepcopy
from datetime import timedelta
from uuid import UUID

import pytest
from conftest import post
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from teaching_assistant.agent.worker import AgentWorker
from teaching_assistant.db import AgentContextCheckpoint, AgentEvent, AgentRun
from teaching_assistant.domain import ModelError, digest
from teaching_assistant.harness import catalog
from teaching_assistant.harness.context import tool_result
from teaching_assistant.harness.context_store import prepare_context
from teaching_assistant.harness.contracts import Reply
from teaching_assistant.harness.rubric import NoArguments, read_question
from teaching_assistant.harness.semantic_compaction import SEMANTIC_TOOL_VERSION

pytestmark = pytest.mark.postgres


def exchange(call_id, name, output):
    return [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": call_id, "function": {"name": name, "arguments": "{}"}, "type": "function"}
            ],
        },
        tool_result(call_id, output),
    ]


async def seed(client, scenario, db, *, oversized_fact=False):
    result = await post(
        client,
        f"/v1/question-versions/{scenario['question']['id']}/rubric-agent-runs",
        {"max_context_characters": 12000},
    )
    run_id = UUID(result["id"])
    async with db.sessions.begin() as session:
        run = await session.get(AgentRun, run_id)
        assert run.manifest["context_version"] == SEMANTIC_TOOL_VERSION
        question = await read_question(session, run, NoArguments())
        if oversized_fact:
            question["reference"] = "不能摘要替代的固定参考答案" * 1800
        run.messages = [
            *run.messages,
            *exchange("old-error", "propose_rubric", {"ok": False, "detail": "error " * 3000}),
            *exchange("question-facts", "read_question", {"ok": True, "result": question}),
            *exchange("recent-error", "propose_rubric", {"ok": False, "detail": "Fix levels."}),
        ]
        run.question_read = True
        run.models_used = run.tools_used = 3
        run.tree_models_used = run.tree_tools_used = 3
        source = deepcopy(run.messages)
    return run_id, source


async def checkpoints(db, run_id):
    async with db.sessions() as session:
        return list(
            await session.scalars(
                select(AgentContextCheckpoint)
                .where(AgentContextCheckpoint.run_id == run_id)
                .order_by(AgentContextCheckpoint.sequence)
            )
        )


async def prepare_directly(db, run_id):
    async with db.sessions.begin() as session:
        run = await session.scalar(select(AgentRun).where(AgentRun.id == run_id).with_for_update())
        return await prepare_context(session, run, catalog.resolve(run.task_type))


async def test_context_checkpoint_dispatch_and_restart_share_one_durable_projection(
    db, settings, client, scenario
):
    run_id, source = await seed(client, scenario, db)
    first = AgentWorker(db, settings, run_ids=(run_id,))
    lease = await first.claim()
    request, proceed = await first.prepare_step(lease)
    assert proceed and request is not None
    checkpoint = (await checkpoints(db, run_id))[0]
    assert checkpoint.messages == request[0]["messages"]
    assert checkpoint.source_hash == digest(source) == checkpoint.details["source_hash"]
    assert checkpoint.details["projection_hash"] == digest(checkpoint.messages)
    assert checkpoint.details["before_characters"] > 12000
    assert checkpoint.details["after_characters"] <= 12000
    assert "old-error" not in {message.get("tool_call_id") for message in request[0]["messages"]}
    async with db.sessions.begin() as session:
        run = await session.get(AgentRun, run_id)
        assert run.messages == source
        event = await session.scalar(
            select(AgentEvent).where(
                AgentEvent.run_id == run_id, AgentEvent.sequence == checkpoint.sequence
            )
        )
        assert event.kind == "context_compacted" and event.body == checkpoint.details
        run.lease_until = (await session.scalar(select(func.clock_timestamp()))) - timedelta(
            seconds=1
        )
    # Process loss after committing the projection and dispatch reservation.
    assert await AgentWorker(db, settings, run_ids=(run_id,)).run_once()
    async with db.sessions() as session:
        run = await session.get(AgentRun, run_id)
        assert run.status == "waiting_teacher" and run.draft_id
        assert run.messages[: len(source)] == source
        assert run.attempts == 2 and run.models_used == 5
        events = list(await session.scalars(select(AgentEvent).where(AgentEvent.run_id == run_id)))
        assert sum(event.kind == "context_compacted" for event in events) == 1
        assert sum(event.kind == "model_dispatched" for event in events) == 2
        assert any(event.kind == "model_unknown" for event in events)
    assert len(await checkpoints(db, run_id)) == 1


async def test_context_checkpoint_source_projection_and_metadata_are_immutable(
    db, client, scenario
):
    run_id, source = await seed(client, scenario, db)
    body = await prepare_directly(db, run_id)
    original = (await checkpoints(db, run_id))[0]
    for statement in (
        "UPDATE agent_context_checkpoints SET source_hash=:value WHERE id=:id",
        "UPDATE agent_context_checkpoints SET messages='[]'::jsonb WHERE id=:id",
        "UPDATE agent_context_checkpoints SET details='{}'::jsonb WHERE id=:id",
        "DELETE FROM agent_context_checkpoints WHERE id=:id",
    ):
        with pytest.raises(DBAPIError):
            async with db.engine.begin() as connection:
                await connection.execute(text(statement), {"id": original.id, "value": "x" * 64})
    saved = (await checkpoints(db, run_id))[0]
    assert saved.source_hash == digest(source)
    assert saved.messages == body["messages"]
    assert saved.details == original.details


async def test_context_checkpoint_and_event_roll_back_with_dispatch_transaction(
    db, client, scenario
):
    run_id, _ = await seed(client, scenario, db)
    with pytest.raises(RuntimeError, match="dispatch transaction aborted"):
        async with db.sessions.begin() as session:
            run = await session.scalar(
                select(AgentRun).where(AgentRun.id == run_id).with_for_update()
            )
            await prepare_context(session, run, catalog.resolve(run.task_type))
            await session.flush()
            raise RuntimeError("dispatch transaction aborted")
    assert not await checkpoints(db, run_id)
    async with db.sessions() as session:
        assert not await session.scalar(
            select(AgentEvent.id).where(
                AgentEvent.run_id == run_id, AgentEvent.kind == "context_compacted"
            )
        )


@pytest.mark.parametrize("corrupted_column", ["source_hash", "messages"])
async def test_checkpoint_columns_must_match_the_rebuild_metadata(
    db, client, scenario, corrupted_column
):
    run_id, _ = await seed(client, scenario, db)
    await prepare_directly(db, run_id)
    valid = (await checkpoints(db, run_id))[0]
    columns = {
        "run_id": run_id,
        "sequence": valid.sequence + 1,
        "source_hash": valid.source_hash,
        "messages": valid.messages,
        "details": valid.details,
    }
    columns[corrupted_column] = "x" * 64 if corrupted_column == "source_hash" else []
    async with db.sessions.begin() as session:
        session.add(AgentContextCheckpoint(**columns))
    with pytest.raises(ModelError) as caught:
        await prepare_directly(db, run_id)
    assert caught.value.code == "agent_context_checkpoint_mismatch"


async def test_duplicate_call_ids_are_checked_against_full_history_after_compaction(
    db, settings, client, scenario, monkeypatch
):
    run_id, source = await seed(client, scenario, db)
    sent = []

    class RepeatedId:
        async def respond(self, body, policy):
            sent.append(body)
            return Reply.model_validate(
                {
                    "tool_calls": [
                        {
                            "id": "old-error",
                            "function": {"name": "read_question", "arguments": "{}"},
                        }
                    ],
                    "metadata": {
                        "requested_model": policy["model"],
                        "returned_model": policy["model"],
                        "adapter_version": "checkpoint-test",
                        "usage": {},
                    },
                }
            )

    monkeypatch.setattr("teaching_assistant.agent.worker.build_model", lambda *_: RepeatedId())
    assert await AgentWorker(db, settings, run_ids=(run_id,)).run_once()
    assert len(sent) == 1
    assert "old-error" not in {
        call["id"] for message in sent[0]["messages"] for call in message.get("tool_calls", [])
    }
    async with db.sessions() as session:
        run = await session.get(AgentRun, run_id)
        assert run.messages == source
        assert run.last_error == "duplicate_tool_call_id"
        assert run.status == "failed" and run.tools_used == 3


async def test_unique_teaching_facts_over_budget_fail_before_model_dispatch(
    db, settings, client, scenario, monkeypatch
):
    run_id, source = await seed(client, scenario, db, oversized_fact=True)

    def forbidden_model(*_):
        raise AssertionError("Context overflow must not construct a model adapter")

    monkeypatch.setattr("teaching_assistant.agent.worker.build_model", forbidden_model)
    assert await AgentWorker(db, settings, run_ids=(run_id,)).run_once()
    async with db.sessions() as session:
        run = await session.get(AgentRun, run_id)
        assert run.last_error == "agent_context_budget_exceeded"
        assert run.status == "failed" and run.models_used == 3
        assert run.messages == source
        assert not await session.scalar(
            select(AgentEvent.id).where(
                AgentEvent.run_id == run_id, AgentEvent.kind == "model_dispatched"
            )
        )
    assert not await checkpoints(db, run_id)


async def test_changed_checkpoint_source_fails_before_next_dispatch(
    db, settings, client, scenario, monkeypatch
):
    run_id, _ = await seed(client, scenario, db)
    await prepare_directly(db, run_id)
    async with db.sessions.begin() as session:
        run = await session.get(AgentRun, run_id)
        messages = deepcopy(run.messages)
        messages[1]["content"] = "Altered after checkpoint"
        run.messages = messages

    def forbidden_model(*_):
        raise AssertionError("A mismatched checkpoint must not dispatch a model request")

    monkeypatch.setattr("teaching_assistant.agent.worker.build_model", forbidden_model)
    assert await AgentWorker(db, settings, run_ids=(run_id,)).run_once()
    async with db.sessions() as session:
        run = await session.get(AgentRun, run_id)
        assert run.last_error == "agent_context_checkpoint_mismatch"
        assert run.status == "failed" and run.models_used == 3
    assert len(await checkpoints(db, run_id)) == 1


@pytest.mark.parametrize("version", [None, "exact-context-v1", "future-context-v999"])
async def test_old_context_policy_is_preserved_and_unknown_version_rejected(
    db, client, scenario, version
):
    run_id, _ = await seed(client, scenario, db)
    async with db.sessions.begin() as session:
        run = await session.get(AgentRun, run_id)
        run.manifest = {**run.manifest, "context_version": version}
    with pytest.raises(ModelError) as caught:
        await prepare_directly(db, run_id)
    assert caught.value.code == (
        "agent_context_version_unsupported"
        if version == "future-context-v999"
        else "agent_context_budget_exceeded"
    )
    assert not await checkpoints(db, run_id)


async def test_protected_recent_group_count_comes_from_the_pinned_manifest(db, client, scenario):
    run_id, _ = await seed(client, scenario, db)
    async with db.sessions.begin() as session:
        run = await session.get(AgentRun, run_id)
        run.manifest = {**run.manifest, "context_keep_recent_groups": 3}
    with pytest.raises(ModelError) as caught:
        await prepare_directly(db, run_id)
    assert caught.value.code == "agent_context_budget_exceeded"
    assert not await checkpoints(db, run_id)

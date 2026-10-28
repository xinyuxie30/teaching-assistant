"""Harness policy, transaction and migration invariants independent of task content."""

import json
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from conftest import post
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from teaching_assistant.agent.context import manifest as legacy_manifest
from teaching_assistant.agent.contracts import Reply, ToolCall
from teaching_assistant.agent.tools import DraftHooks
from teaching_assistant.agent.worker import AgentWorker
from teaching_assistant.db import AgentArtifact, AgentRun, Receipt, RubricDraft
from teaching_assistant.demo import DEMO_RUBRIC
from teaching_assistant.domain import DomainError, ModelMetadata, digest
from teaching_assistant.harness import catalog
from teaching_assistant.harness.rubric import REGISTRY, RubricTask
from teaching_assistant.harness.tools import ToolRegistry


async def start(client, scenario, **options):
    return await post(
        client, f"/v1/question-versions/{scenario['question']['id']}/rubric-agent-runs", options
    )


async def start_with_legacy_hooks(client, scenario, settings, **options):
    policy = catalog.v4_manifest(settings, "rubric", "fake-rubric-agent-v1")
    old_profile = {
        "id": "agent-" + digest(policy),
        "alias": policy["alias"],
        "manifest": policy,
        "ready": True,
    }
    with patch("teaching_assistant.agent.service.profiles", return_value=[old_profile]):
        return await start(client, scenario, **options)


async def read(client, run_id):
    response = await client.get(f"/v1/agent-runs/{run_id}")
    assert response.status_code == 200, response.text
    return response.json()


async def tool_outputs(client, run_id):
    response = await client.get(f"/v1/agent-runs/{run_id}/events")
    assert response.status_code == 200, response.text
    return [
        event["body"]["output"]
        for event in response.json()["items"]
        if event["kind"] == "post_tool"
    ]


def response(*, name=None, arguments=None, content=None):
    return Reply(
        content=content,
        tool_calls=[
            ToolCall.model_validate(
                {
                    "id": "call-" + uuid4().hex,
                    "function": {"name": name, "arguments": json.dumps(arguments or {})},
                }
            )
        ]
        if name
        else [],
        metadata=ModelMetadata(
            requested_model="fake-rubric-agent-v1",
            returned_model="fake-rubric-agent-v1",
            adapter_version="harness-test",
            usage={},
        ),
    )


class PermissiveHooks(DraftHooks):
    def pre_tool(self, name, context):
        return None

    def post_tool(self, name, result):
        pass

    def stop(self, context):
        return True


def test_registry_rejects_duplicate_names_and_owns_registered_tools():
    tool = REGISTRY.get("read_question")
    with pytest.raises(ValueError, match="unique"):
        ToolRegistry([tool, replace(tool, description="An ambiguous second handler")])
    source = [tool]
    registry = ToolRegistry(source)
    source.append(REGISTRY.get("propose_rubric"))
    with pytest.raises(DomainError) as error:
        registry.get("propose_rubric")
    assert error.value.code == "tool_not_allowed"


@pytest.mark.parametrize(
    "arguments",
    [
        '{"scope": 1, "scope": 2}',
        '{"scope": NaN}',
        '{"scope": Infinity}',
        '{"teacher_id": "model-supplied-owner"}',
        "[]",
    ],
)
def test_registry_rejects_ambiguous_or_scope_changing_arguments(arguments):
    call = ToolCall.model_validate(
        {"id": "bad-input", "function": {"name": "read_question", "arguments": arguments}}
    )
    with pytest.raises(DomainError) as error:
        REGISTRY.validate_call(call)
    assert error.value.code == "invalid_tool_arguments"
    assert "model-supplied-owner" not in error.value.detail


@pytest.mark.postgres
async def test_permissive_hook_cannot_skip_the_tasks_read_requirement(
    db, settings, client, scenario, monkeypatch
):
    class EarlyWrite:
        async def respond(self, body, policy):
            return response(name="propose_rubric", arguments={"rubric": DEMO_RUBRIC})

    monkeypatch.setattr("teaching_assistant.agent.worker.build_model", lambda *_: EarlyWrite())
    run = await start_with_legacy_hooks(client, scenario, settings, max_tool_calls=1)
    await AgentWorker(db, settings, run_ids=(UUID(run["id"]),), hooks=PermissiveHooks()).run_once()
    saved = await read(client, run["id"])
    assert saved["status"] == "failed" and saved["draft_id"] is None
    assert saved["models_used"] == saved["tools_used"] == 1
    assert (await tool_outputs(client, run["id"]))[0]["error_code"] == "read_question_required"
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(RubricDraft)) == 1


@pytest.mark.postgres
@pytest.mark.parametrize("corruption", ["handler", "hook_confirmation", "hook_identity"])
async def test_typed_output_failure_rolls_back_handler_effects_even_with_permissive_hooks(
    db, settings, client, scenario, monkeypatch, corruption
):
    draft_tool = REGISTRY.get("propose_rubric")

    async def corrupt_handler(session, run, arguments):
        saved = await draft_tool.handler(session, run, arguments)
        return {**saved, "confirmed": True}

    class CorruptingHook(PermissiveHooks):
        def post_tool(self, name, result):
            if name == "propose_rubric":
                if corruption == "hook_identity":
                    # A different well-formed UUID passes the output schema but changes the effect.
                    result["draft_id"] = str(uuid4())
                else:
                    result["confirmed"] = True

    if corruption == "handler":
        monkeypatch.setattr(
            RubricTask,
            "registry",
            ToolRegistry(
                [REGISTRY.get("read_question"), replace(draft_tool, handler=corrupt_handler)]
            ),
        )
    run = await start_with_legacy_hooks(client, scenario, settings, max_tool_calls=2)
    await AgentWorker(
        db,
        settings,
        run_ids=(UUID(run["id"]),),
        hooks=PermissiveHooks() if corruption == "handler" else CorruptingHook(),
    ).run_once()
    saved = await read(client, run["id"])
    assert saved["status"] == "failed" and saved["draft_id"] is None
    assert saved["models_used"] == saved["tools_used"] == 2
    assert (await tool_outputs(client, run["id"]))[-1]["error_code"] == "invalid_tool_result"
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(RubricDraft)) == 1


@pytest.mark.postgres
@pytest.mark.parametrize("timed_out", [False, True])
async def test_failed_state_update_rolls_back_without_losing_tool_receipts(
    db, settings, client, scenario, monkeypatch, timed_out
):
    original = RubricTask.apply_result

    async def failing_apply_result(self, session, run, name, result):
        await original(self, session, run, name, result)
        if name == "propose_rubric":
            run.task_state = {"partial_artifact": result["draft_id"]}
            await session.flush()
            if timed_out:
                raise TimeoutError()
            raise DomainError("task_state_rejected", "Task state rejected after draft creation")

    monkeypatch.setattr(RubricTask, "apply_result", failing_apply_result)
    run = await start(client, scenario, max_tool_calls=2)
    assert await AgentWorker(db, settings, run_ids=(UUID(run["id"]),)).run_once()
    saved = await read(client, run["id"])
    assert saved["status"] == "failed" and saved["draft_id"] is None
    assert saved["error_code"] == "agent_tool_budget_exhausted"
    assert saved["models_used"] == saved["tools_used"] == 2
    outputs = await tool_outputs(client, run["id"])
    assert len(outputs) == 2 and outputs[0]["ok"] is True
    assert outputs[1]["error_code"] == ("tool_timeout" if timed_out else "task_state_rejected")
    async with db.sessions() as session:
        row = await session.get(AgentRun, UUID(run["id"]))
        assert row.task_state == {} and row.pending_calls == []
        calls = [call["id"] for message in row.messages for call in message.get("tool_calls", [])]
        results = [message["tool_call_id"] for message in row.messages if message["role"] == "tool"]
        assert len(calls) == len(set(calls)) == 2 and calls == results
        assert await session.scalar(select(func.count()).select_from(RubricDraft)) == 1


@pytest.mark.postgres
@pytest.mark.parametrize("invalid_artifact", ["already_confirmed", "other_question"])
async def test_stop_requires_an_unconfirmed_artifact_in_the_run_scope(
    db, settings, client, scenario, monkeypatch, invalid_artifact
):
    class ClaimCompletion:
        async def respond(self, body, policy):
            return response(content="The task is complete.")

    artifact_id = scenario["draft"]["id"]
    if invalid_artifact == "other_question":
        question = await post(
            client,
            f"/v1/assignments/{scenario['assignment']['id']}/question-versions",
            {
                "question_key": "different-question",
                "prompt": "A different question",
                "reference": "A different reference",
                "maximum_points": "10.00",
            },
        )
        draft = await post(
            client,
            f"/v1/question-versions/{question['id']}/rubric-drafts",
            {"rubric": DEMO_RUBRIC},
        )
        artifact_id = draft["id"]
    run = await start_with_legacy_hooks(client, scenario, settings)
    async with db.sessions.begin() as session:
        row = await session.get(AgentRun, UUID(run["id"]))
        row.question_read = True
        row.draft_id = UUID(artifact_id)
    monkeypatch.setattr("teaching_assistant.agent.worker.build_model", lambda *_: ClaimCompletion())
    await AgentWorker(db, settings, run_ids=(UUID(run["id"]),), hooks=PermissiveHooks()).run_once()
    saved = await read(client, run["id"])
    assert saved["status"] == "failed" and saved["error_code"] == "agent_missing_draft"
    assert saved["models_used"] == 2 and saved["tools_used"] == 0


@pytest.mark.postgres
async def test_recovery_rejects_a_valid_profile_for_the_wrong_task_before_dispatch(
    db, settings, client, scenario, monkeypatch
):
    run = await start(client, scenario)
    worker = AgentWorker(db, settings, run_ids=(UUID(run["id"]),))
    profile_id, policy = next(
        (profile_id, policy)
        for profile_id, policy in worker.available.items()
        if policy.get("task_type") == "lesson-plan"
    )
    async with db.sessions.begin() as session:
        row = await session.get(AgentRun, UUID(run["id"]))
        row.profile_id, row.manifest = profile_id, policy

    def forbidden_dispatch(*args):
        pytest.fail("A mismatched task must be rejected before building a model")

    monkeypatch.setattr("teaching_assistant.agent.worker.build_model", forbidden_dispatch)
    assert not await worker.run_once()
    saved = await read(client, run["id"])
    assert saved["status"] == "failed" and saved["error_code"] == "agent_profile_mismatch"
    assert saved["models_used"] == saved["tools_used"] == 0


@pytest.mark.postgres
async def test_0006_upgrade_preserves_pending_calls_receipts_and_recovery(
    db, settings, client, scenario, monkeypatch
):
    path = f"/v1/question-versions/{scenario['question']['id']}/rubric-agent-runs"
    policy = legacy_manifest(settings, "fake-rubric-agent-v1")
    assert "task_type" not in policy
    monkeypatch.setattr(
        "teaching_assistant.agent.service.profiles",
        lambda *_: [
            {
                "alias": "fake-rubric-agent-v1",
                "id": "agent-" + digest(policy),
                "manifest": policy,
                "ready": True,
            }
        ],
    )
    run = await post(client, path, {}, key="legacy-pending-run")
    first = AgentWorker(db, settings, run_ids=(UUID(run["id"]),))
    lease = await first.claim()
    request, _ = await first.prepare_step(lease)
    assert await first.save_reply(lease, await first.invoke(lease, *request))
    async with db.sessions.begin() as session:
        row = await session.get(AgentRun, lease.run_id)
        row.lease_until = (await session.scalar(select(func.clock_timestamp()))) - timedelta(
            seconds=1
        )
        checkpoint = deepcopy((row.messages, row.pending_calls, row.manifest, row.models_used))
        receipt = await session.scalar(select(Receipt).where(Receipt.key == "legacy-pending-run"))
        receipt_hash = receipt.request_hash

    def round_trip(connection):
        config = Config("alembic.ini")
        config.attributes["connection"] = connection
        command.downgrade(config, "0006")
        command.upgrade(config, "head")
        command.check(config)

    async with db.engine.begin() as connection:
        await connection.run_sync(round_trip)
    async with db.sessions() as session:
        row = await session.get(AgentRun, lease.run_id)
        assert row.task_type == "rubric" and row.task_input == row.task_state == {}
        assert (row.messages, row.pending_calls, row.manifest, row.models_used) == checkpoint
        receipt = await session.scalar(select(Receipt).where(Receipt.key == "legacy-pending-run"))
        assert receipt.request_hash == receipt_hash
    assert await post(client, path, {}, key="legacy-pending-run") == run
    assert await AgentWorker(db, settings, run_ids=(lease.run_id,)).run_once()
    saved = await read(client, run["id"])
    assert saved["status"] == "waiting_teacher" and saved["attempts"] == 2
    assert saved["models_used"] == saved["tools_used"] == 2
    await first.execute(lease)
    assert await read(client, run["id"]) == saved


@pytest.mark.postgres
@pytest.mark.parametrize("protected_record", ["generic_task", "artifact"])
async def test_0007_downgrade_refuses_to_discard_generic_work(
    db, client, scenario, protected_record
):
    run = await start(client, scenario)
    async with db.sessions.begin() as session:
        rubric_run = await session.get(AgentRun, UUID(run["id"]))
        if protected_record == "generic_task":
            protected = AgentRun(
                teacher_id=rubric_run.teacher_id,
                task_type="lesson-plan",
                question_version_id=None,
                task_input={"batch_id": scenario["batch"]["id"]},
                profile_id=rubric_run.profile_id,
                manifest=rubric_run.manifest,
                limits=rubric_run.limits,
                messages=[],
                expires_at=rubric_run.expires_at,
            )
        else:
            body = {"title": "A draft that must survive a migration rollback"}
            protected = AgentArtifact(
                run_id=rubric_run.id, kind="lesson-plan", body=body, content_hash=digest(body)
            )
        session.add(protected)
        await session.flush()
        protected_id = protected.id

    def downgrade(connection):
        config = Config("alembic.ini")
        config.attributes["connection"] = connection
        command.downgrade(config, "0006")

    with pytest.raises(DBAPIError, match="Cannot downgrade while generic harness"):
        async with db.engine.begin() as connection:
            await connection.run_sync(downgrade)
    async with db.sessions() as session:
        assert await session.scalar(text("SELECT version_num FROM alembic_version")) == "0013"
        record_type = AgentRun if protected_record == "generic_task" else AgentArtifact
        assert await session.get(record_type, protected_id) is not None

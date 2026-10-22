"""Pinned registered hooks remain deny-only across dispatch and process recovery."""

from copy import deepcopy
from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select
from teaching_assistant.db import AgentEvent, AgentRun, RubricDraft
from teaching_assistant.domain import DomainError, digest
from teaching_assistant.harness import catalog, hooks
from teaching_assistant.harness.worker import HarnessWorker
from test_harness_core import read, start, tool_outputs


def test_v5_profile_pins_normalized_detached_hook_config(settings):
    settings.harness_hook_config = {"denied_tools": ["propose_rubric"]}
    policy = catalog.manifest(settings, "rubric", "fake-rubric-agent-v1")
    saved = policy["hook_snapshot"]
    expected = {
        "id": "deny-tools",
        "version": "1",
        "config": {"denied_tools": ["propose_rubric"], "block_stop": False},
    }
    assert saved == {**expected, "content_hash": digest(expected)}
    assert policy["runtime_version"] == "harness-runtime-v5"
    settings.harness_hook_config["denied_tools"].clear()
    assert saved["config"]["denied_tools"] == ["propose_rubric"]
    changed = catalog.manifest(settings, "rubric", "fake-rubric-agent-v1")
    assert digest(changed) != digest(policy)
    restored = hooks.build(saved)
    assert restored.pre_tool("propose_rubric", {}) == "configured_hook_denied"
    assert restored.pre_tool("read_question", {}) is None
    assert restored.stop({}) is True
    assert "hook_snapshot" not in catalog.v4_manifest(settings, "rubric", policy["alias"])


@pytest.mark.parametrize("corruption", ["config", "version", "registry", "hash"])
def test_hook_restore_rejects_unverifiable_configuration(settings, corruption):
    saved = hooks.snapshot(settings)
    if corruption == "config":
        saved["config"]["block_stop"] = True
    elif corruption == "version":
        saved["version"] = "unavailable-implementation"
    elif corruption == "registry":
        saved["id"] = "model-supplied-module"
    else:
        saved["content_hash"] = "0" * 64
    with pytest.raises(DomainError) as error:
        hooks.build(saved)
    assert error.value.code == "agent_hook_mismatch"


@pytest.mark.parametrize(
    ("identifier", "config", "code"),
    [
        ("unregistered", {}, "unknown_hook_profile"),
        ("deny-tools", {"allow_write": True}, "invalid_hook_config"),
    ],
)
def test_hook_configuration_requires_registered_schema(settings, identifier, config, code):
    settings.harness_hook_id, settings.harness_hook_config = identifier, config
    with pytest.raises(DomainError) as error:
        hooks.snapshot(settings)
    assert error.value.code == code


async def assert_no_dispatch(db, run_id):
    async with db.sessions() as session:
        run = await session.get(AgentRun, run_id)
        assert run.models_used == run.tools_used == 0
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AgentEvent)
                .where(AgentEvent.run_id == run_id, AgentEvent.kind == "model_dispatched")
            )
            == 0
        )


def forbidden_model(*args):
    pytest.fail("A mismatched hook policy must be rejected before constructing a model")


@pytest.mark.postgres
@pytest.mark.parametrize("denied_tool", ["read_question", "propose_rubric"])
async def test_registered_hook_denial_is_paired_without_tool_effects(
    db, settings, client, scenario, denied_tool
):
    settings.harness_hook_config = {"denied_tools": [denied_tool]}
    maximum = 1 if denied_tool == "read_question" else 2
    created = await start(client, scenario, max_tool_calls=maximum)
    run_id = UUID(created["id"])
    assert await HarnessWorker(db, settings, run_ids=(run_id,)).run_once()
    saved = await read(client, created["id"])
    assert saved["status"] == "failed" and saved["draft_id"] is None
    assert saved["tools_used"] == saved["models_used"] == maximum
    outputs = await tool_outputs(client, created["id"])
    assert len(outputs) == maximum
    assert outputs[-1]["ok"] is False
    assert outputs[-1]["error_code"] == "configured_hook_denied"
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(RubricDraft)) == 1
        run = await session.get(AgentRun, run_id)
        calls = [call["id"] for message in run.messages for call in message.get("tool_calls", [])]
        receipts = [
            message["tool_call_id"] for message in run.messages if message["role"] == "tool"
        ]
        assert calls == receipts and len(set(calls)) == maximum


@pytest.mark.postgres
async def test_registered_stop_hook_keeps_the_draft_unconfirmed(db, settings, client, scenario):
    settings.harness_hook_config = {"block_stop": True}
    created = await start(client, scenario)
    run_id = UUID(created["id"])
    assert await HarnessWorker(db, settings, run_ids=(run_id,)).run_once()
    saved = await read(client, created["id"])
    assert saved["status"] == "failed" and saved["error_code"] == "agent_stop_blocked"
    assert saved["models_used"] == saved["tools_used"] == 2
    async with db.sessions() as session:
        run = await session.get(AgentRun, run_id)
        draft = await session.get(RubricDraft, run.draft_id)
        assert draft is not None and draft.confirmed_version_id is None
        events = list(
            await session.scalars(
                select(AgentEvent).where(
                    AgentEvent.run_id == run_id, AgentEvent.kind == "stop_hook"
                )
            )
        )
        assert len(events) == 1 and events[0].body["accepted"] is False


@pytest.mark.postgres
async def test_arbitrary_constructor_hook_cannot_override_v5(
    db, settings, client, scenario, monkeypatch
):
    created = await start(client, scenario)
    run_id = UUID(created["id"])
    monkeypatch.setattr(HarnessWorker, "build_model", forbidden_model)
    assert not await HarnessWorker(db, settings, run_ids=(run_id,), hooks=object()).run_once()
    saved = await read(client, created["id"])
    assert saved["status"] == "failed" and saved["error_code"] == "agent_hook_mismatch"
    await assert_no_dispatch(db, run_id)


@pytest.mark.postgres
async def test_restart_with_changed_hook_config_cannot_resume_old_profile(
    db, settings, client, scenario, monkeypatch
):
    settings.harness_hook_config = {"denied_tools": ["propose_rubric"]}
    original_settings = settings.model_copy(deep=True)
    created = await start(client, scenario, max_tool_calls=2)
    run_id = UUID(created["id"])
    lease = await HarnessWorker(db, settings, run_ids=(run_id,)).claim()
    assert lease is not None
    async with db.sessions.begin() as session:
        run = await session.get(AgentRun, run_id)
        original_policy = deepcopy(run.manifest)
        run.lease_until = await session.scalar(select(func.clock_timestamp())) - timedelta(
            seconds=1
        )
    settings.harness_hook_config = {}
    with monkeypatch.context() as context:
        context.setattr(HarnessWorker, "build_model", forbidden_model)
        assert not await HarnessWorker(db, settings, run_ids=(run_id,)).run_once()
    await assert_no_dispatch(db, run_id)
    async with db.sessions() as session:
        run = await session.get(AgentRun, run_id)
        assert run.status == "running" and run.epoch == lease.epoch
        assert run.manifest == original_policy
    # A worker with the pinned profile resumes the lease and still applies its denial.
    assert await HarnessWorker(db, original_settings, run_ids=(run_id,)).run_once()
    assert (await tool_outputs(client, created["id"]))[-1]["error_code"] == "configured_hook_denied"


@pytest.mark.postgres
@pytest.mark.parametrize("rehash_snapshot", [False, True])
async def test_tampered_saved_config_cannot_dispatch_even_with_recomputed_hook_hash(
    db, settings, client, scenario, monkeypatch, rehash_snapshot
):
    created = await start(client, scenario)
    run_id = UUID(created["id"])
    async with db.sessions.begin() as session:
        run = await session.get(AgentRun, run_id)
        policy = deepcopy(run.manifest)
        saved = policy["hook_snapshot"]
        saved["config"]["block_stop"] = True
        if rehash_snapshot:
            saved["content_hash"] = digest({key: saved[key] for key in ("id", "version", "config")})
        run.manifest = policy
    monkeypatch.setattr(HarnessWorker, "build_model", forbidden_model)
    assert not await HarnessWorker(db, settings, run_ids=(run_id,)).run_once()
    saved = await read(client, created["id"])
    assert saved["status"] == "failed" and saved["error_code"] == "agent_profile_mismatch"
    await assert_no_dispatch(db, run_id)

"""Migration reversibility stops before discarding task-tree or context history."""

from copy import deepcopy
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from conftest import post
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from teaching_assistant.db import AgentContextCheckpoint, AgentRun, Receipt
from teaching_assistant.domain import digest

pytestmark = pytest.mark.postgres


def config_for(connection):
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    return config


async def root_run(client, scenario, *, key=None):
    return await post(
        client,
        f"/v1/question-versions/{scenario['question']['id']}/rubric-agent-runs",
        {},
        key=key,
    )


async def test_0008_round_trip_preserves_root_checkpoint_and_seeds_tree_usage(db, client, scenario):
    created = await root_run(client, scenario, key="tree-migration-root")
    run_id = UUID(created["id"])
    async with db.sessions.begin() as session:
        root = await session.get(AgentRun, run_id)
        root.models_used = 3
        root.tools_used = 2
        root.tree_models_used = 3
        root.tree_tools_used = 2
        root.inflight_seq = 7
        root.pending_calls = [
            {
                "id": "durable-call",
                "type": "function",
                "function": {"name": "read_question", "arguments": "{}"},
            }
        ]
        root.messages = [*root.messages, {"role": "assistant", "tool_calls": root.pending_calls}]
        before = deepcopy(
            (root.manifest, root.messages, root.pending_calls, root.limits, root.inflight_seq)
        )
        receipt = await session.scalar(select(Receipt).where(Receipt.key == "tree-migration-root"))
        receipt_hash = receipt.request_hash

    def round_trip(connection):
        config = config_for(connection)
        command.downgrade(config, "0007")
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0007"
        command.upgrade(config, "head")
        command.check(config)

    async with db.engine.begin() as connection:
        await connection.run_sync(round_trip)
    async with db.sessions() as session:
        root = await session.get(AgentRun, run_id)
        assert root.parent_run_id is root.root_run_id is root.parent_tool_call_id is None
        assert root.depth == root.children_spawned == 0
        assert root.tree_models_used == root.models_used == 3
        assert root.tree_tools_used == root.tools_used == 2
        assert not root.resume_pending
        assert (
            root.manifest,
            root.messages,
            root.pending_calls,
            root.limits,
            root.inflight_seq,
        ) == before
        receipt = await session.scalar(select(Receipt).where(Receipt.key == "tree-migration-root"))
        assert receipt.request_hash == receipt_hash
    assert await root_run(client, scenario, key="tree-migration-root") == created


@pytest.mark.parametrize("protected", ["child", "context", "waiting_parent"])
async def test_0008_downgrade_refuses_to_discard_new_history(db, client, scenario, protected):
    created = await root_run(client, scenario)
    run_id = UUID(created["id"])
    async with db.sessions.begin() as session:
        root = await session.get(AgentRun, run_id)
        if protected == "child":
            record = AgentRun(
                teacher_id=root.teacher_id,
                question_version_id=root.question_version_id,
                task_type=root.task_type,
                task_input={},
                parent_run_id=root.id,
                root_run_id=root.id,
                parent_tool_call_id="saved-delegation",
                depth=1,
                profile_id=root.profile_id,
                manifest=root.manifest,
                limits=root.limits,
                messages=[],
                expires_at=root.expires_at,
            )
            session.add(record)
        elif protected == "context":
            record = AgentContextCheckpoint(
                run_id=root.id,
                sequence=1,
                source_hash=digest(root.messages),
                messages=deepcopy(root.messages),
                details={"reason": "saved context audit history"},
            )
            session.add(record)
        else:
            root.status = "waiting_children"
            record = root
        await session.flush()
        record_id = record.id

    def downgrade(connection):
        command.downgrade(config_for(connection), "0007")

    with pytest.raises(
        DBAPIError, match="Cannot downgrade while harness child tasks or context checkpoints exist"
    ):
        async with db.engine.begin() as connection:
            await connection.run_sync(downgrade)
    async with db.sessions() as session:
        assert await session.scalar(text("SELECT version_num FROM alembic_version")) == "0013"
        record_type = AgentContextCheckpoint if protected == "context" else AgentRun
        record = await session.get(record_type, record_id)
        assert record is not None
        if protected == "waiting_parent":
            assert record.status == "waiting_children"


@pytest.mark.parametrize("field", ["parent_run_id", "root_run_id", "parent_tool_call_id", "depth"])
async def test_0008_persisted_child_ancestry_is_immutable(db, client, scenario, field):
    created = await root_run(client, scenario)
    run_id = UUID(created["id"])
    async with db.sessions.begin() as session:
        root = await session.get(AgentRun, run_id)
        child = AgentRun(
            teacher_id=root.teacher_id,
            question_version_id=root.question_version_id,
            task_type=root.task_type,
            parent_run_id=root.id,
            root_run_id=root.id,
            parent_tool_call_id="immutable-parent-call",
            depth=1,
            profile_id=root.profile_id,
            manifest=root.manifest,
            limits=root.limits,
            messages=[],
            expires_at=root.expires_at,
        )
        session.add(child)
        await session.flush()
        child_id = child.id
    with pytest.raises(DBAPIError, match="Harness task ancestry is immutable"):
        async with db.sessions.begin() as session:
            child = await session.get(AgentRun, child_id)
            setattr(child, field, 2 if field == "depth" else None)
    async with db.sessions() as session:
        child = await session.get(AgentRun, child_id)
        assert child.parent_run_id == child.root_run_id == run_id
        assert child.parent_tool_call_id == "immutable-parent-call"
        assert child.depth == 1

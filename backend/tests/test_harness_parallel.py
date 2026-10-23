"""Parallel dispatch, ordered joins, depth-two recovery, and subtree fencing."""

import asyncio
import json
from datetime import timedelta
from uuid import UUID

import pytest
from conftest import post
from sqlalchemy import func, select
from teaching_assistant.db import AgentArtifact, AgentEvent, AgentRun
from teaching_assistant.domain import DomainError, digest
from teaching_assistant.harness import catalog, scheduler
from teaching_assistant.harness.contracts import Reply, ToolCall
from teaching_assistant.harness.delegation import ChildTaskRequest
from teaching_assistant.harness.parallel_lesson import ParallelLessonTask
from teaching_assistant.harness.worker import HarnessWorker, LostAgentLease

pytestmark = pytest.mark.postgres

BRIEF = {
    "topic": "可复核的版本记录",
    "objectives": ["说明固定记录与可追溯来源的关系"],
    "duration_minutes": 10,
}


@pytest.fixture
async def parallel_course(client):
    result = await post(
        client,
        "/v1/workspaces",
        {
            "course_name": "Parallel harness course",
            "classroom_name": "Parallel classroom",
            "students": [{"external_id": "parallel-1", "name": "Student"}],
        },
    )
    return result["course_id"]


async def start(client, course, **limits):
    return await post(
        client,
        f"/v1/courses/{course}/lesson-agent-runs",
        {
            "brief": BRIEF,
            "agent_profile": "fake-lesson-parallel-agent-v1",
            "max_model_calls": 12,
            "max_tool_calls": 24,
            **limits,
        },
    )


async def rows(db, root_id):
    async with db.sessions() as session:
        return list(
            await session.scalars(
                select(AgentRun)
                .where(
                    (AgentRun.id == UUID(str(root_id)))
                    | (AgentRun.root_run_id == UUID(str(root_id)))
                )
                .order_by(AgentRun.depth, AgentRun.created_at, AgentRun.id)
            )
        )


async def detail(client, run_id):
    response = await client.get(f"/v1/agent-runs/{run_id}")
    assert response.status_code == 200, response.text
    return response.json()


async def fanout(db, settings, client, course, **limits):
    root = await start(client, course, **limits)
    assert await HarnessWorker(db, settings, run_ids=(UUID(root["id"]),)).run_once()
    tree = await rows(db, root["id"])
    assert len(tree) == 3
    assert tree[0].status == "waiting_children"
    children = {item.task_type: item for item in tree[1:]}
    return root, children["lesson-practice-designer"], children["lesson-review"]


async def nested(db, settings, client, course, **limits):
    root, designer, review = await fanout(db, settings, client, course, **limits)
    assert await HarnessWorker(db, settings, run_ids=(designer.id,)).run_once()
    leaf = next(item for item in await rows(db, root["id"]) if item.depth == 2)
    return root, designer, review, leaf


async def drain(db, settings, root_id):
    worker = HarnessWorker(db, settings, run_ids=(UUID(str(root_id)),))
    for _ in range(16):
        if not await worker.run_once():
            return
    pytest.fail("The bounded parallel fixture did not become idle")


@pytest.mark.parametrize("lost_calls", [1, 2])
async def test_nested_designer_recovers_lost_calls_within_original_tree_budget(
    db, settings, client, parallel_course, lost_calls
):
    root, designer, _ = await fanout(db, settings, client, parallel_course)
    for _ in range(lost_calls):
        worker = HarnessWorker(db, settings, run_ids=(designer.id,))
        lease = await worker.claim()
        request, proceed = await worker.prepare_step(lease)
        assert proceed and request is not None
        # The dispatch is durable but the process never records a response.
        async with db.sessions.begin() as session:
            row = await session.get(AgentRun, designer.id)
            row.lease_until = await session.scalar(select(func.clock_timestamp())) - timedelta(
                seconds=1
            )
    await drain(db, settings, root["id"])
    tree = await rows(db, root["id"])
    assert tree[0].status == "waiting_teacher"
    recovered = next(row for row in tree if row.id == designer.id)
    assert recovered.status == "completed"
    assert recovered.models_used == 3 + lost_calls
    assert recovered.attempts == 1 + lost_calls
    assert tree[0].tree_models_used == sum(row.models_used for row in tree) == 10 + lost_calls
    assert tree[0].limits["max_model_calls"] == 12
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AgentArtifact)) == 4
        unknown = await session.scalar(
            select(func.count())
            .select_from(AgentEvent)
            .where(AgentEvent.run_id == designer.id, AgentEvent.kind == "model_unknown")
        )
        assert unknown == lost_calls


async def test_parallel_children_and_grandchild_produce_one_reviewable_artifact(
    db,
    settings,
    client,
    parallel_course,
):
    root, designer, review = await fanout(db, settings, client, parallel_course)
    assert designer.status == review.status == "queued"
    assert designer.parent_tool_call_id != review.parent_tool_call_id
    await drain(db, settings, root["id"])
    tree = await rows(db, root["id"])
    assert len(tree) == 4 and [item.depth for item in tree] == [0, 1, 1, 2]
    parent = tree[0]
    assert parent.status == "waiting_teacher"
    assert parent.children_spawned == 3
    assert parent.tree_models_used == sum(item.models_used for item in tree) == 10
    assert parent.tree_tools_used == sum(item.tools_used for item in tree) == 11
    assert all(item.status == "completed" for item in tree[1:])
    assert all(item.attempts == 1 for item in tree)
    for item in tree:
        calls = [call["id"] for message in item.messages for call in message.get("tool_calls", [])]
        results = [
            message["tool_call_id"] for message in item.messages if message["role"] == "tool"
        ]
        assert calls == results and len(calls) == len(set(calls))
        assert item.pending_calls == []
    async with db.sessions() as session:
        artifacts = {item.kind: item for item in await session.scalars(select(AgentArtifact))}
        assert len(artifacts) == 4
        plan = artifacts["lesson-plan-parallel"]
        assert plan.body["practice"] == artifacts["lesson-practice"].body["practice"]
        assert plan.body["review"] == artifacts["lesson-review"].body
        assert all(item.confirmed_at is None for item in artifacts.values())
    confirmed = await post(
        client, f"/v1/agent-runs/{root['id']}/confirm", {"content_hash": plan.content_hash}
    )
    assert confirmed["status"] == "completed"
    saved_artifact = await client.get(f"/v1/agent-runs/{root['id']}/artifact")
    assert saved_artifact.json()["confirmed_at"] is not None


async def test_out_of_order_completion_waits_for_all_children_then_delivers_once(
    db,
    settings,
    client,
    parallel_course,
):
    root, designer, review = await fanout(db, settings, client, parallel_course)
    assert await HarnessWorker(db, settings, run_ids=(review.id,)).run_once()
    worker = HarnessWorker(db, settings)
    async with db.sessions.begin() as session:
        _, parent = await scheduler.lock_run(session, UUID(root["id"]))
        assert not await worker.resume_parent(session, parent)
        assert len(parent.pending_calls) == 2 and parent.status == "waiting_children"
        assert not parent.task_state.get("review_child")
    assert await HarnessWorker(db, settings, run_ids=(designer.id,)).run_once()
    leaf = next(item for item in await rows(db, root["id"]) if item.depth == 2)
    assert await HarnessWorker(db, settings, run_ids=(leaf.id,)).run_once()
    assert await HarnessWorker(db, settings, run_ids=(designer.id,)).run_once()
    claims = await asyncio.gather(
        *(HarnessWorker(db, settings, run_ids=(UUID(root["id"]),)).run_once() for _ in range(2))
    )
    assert sum(claims) == 1
    parent = (await rows(db, root["id"]))[0]
    assert parent.status == "waiting_teacher" and parent.attempts == 1
    async with db.sessions() as session:
        receipts = list(
            await session.scalars(
                select(AgentEvent)
                .where(AgentEvent.run_id == parent.id, AgentEvent.kind == "child_received")
                .order_by(AgentEvent.sequence)
            )
        )
        assert [item.body["child_run_id"] for item in receipts] == [
            str(designer.id),
            str(review.id),
        ]


async def test_waiting_ancestors_release_slots_for_grandchild(
    db,
    settings,
    client,
    parallel_course,
):
    root, designer, review = await fanout(db, settings, client, parallel_course)
    review_worker = HarnessWorker(db, settings, run_ids=(review.id,))
    review_lease = await review_worker.claim()
    assert review_lease is not None
    assert await HarnessWorker(db, settings, run_ids=(designer.id,)).run_once()
    leaf = next(item for item in await rows(db, root["id"]) if item.depth == 2)
    leaf_worker = HarnessWorker(db, settings, run_ids=(leaf.id,))
    leaf_lease = await leaf_worker.claim()
    assert leaf_lease is not None
    tree = await rows(db, root["id"])
    assert sum(item.status == "running" for item in tree) == 2
    assert sum(item.status == "waiting_children" for item in tree) == 2
    await asyncio.gather(review_worker.execute(review_lease), leaf_worker.execute(leaf_lease))
    await drain(db, settings, root["id"])
    assert (await detail(client, root["id"]))["status"] == "waiting_teacher"


async def test_partial_join_survives_restart_without_replaying_received_child(
    db,
    settings,
    client,
    parallel_course,
):
    root, designer, review, leaf = await nested(db, settings, client, parallel_course)
    assert await HarnessWorker(db, settings, run_ids=(leaf.id,)).run_once()
    assert await HarnessWorker(db, settings, run_ids=(designer.id,)).run_once()
    async with db.sessions.begin() as session:
        _, parent = await scheduler.lock_run(session, UUID(root["id"]))
        assert not await HarnessWorker(db, settings).resume_parent(session, parent)
        assert parent.status == "waiting_children" and len(parent.pending_calls) == 1
        assert parent.resume_pending
        assert parent.task_state["designer_child"]["child_run_id"] == str(designer.id)
    async with db.sessions.begin() as session:
        _, parent = await scheduler.lock_run(session, UUID(root["id"]))
        assert not await HarnessWorker(db, settings).resume_parent(session, parent)
        assert len(parent.pending_calls) == 1
    assert await HarnessWorker(db, settings, run_ids=(review.id,)).run_once()
    await drain(db, settings, root["id"])
    parent = (await rows(db, root["id"]))[0]
    assert parent.status == "waiting_teacher" and parent.attempts == 1
    results = [item["tool_call_id"] for item in parent.messages if item["role"] == "tool"]
    assert (
        results.count(designer.parent_tool_call_id)
        == results.count(review.parent_tool_call_id)
        == 1
    )


async def test_failed_dispatch_rolls_back_only_that_child_and_keeps_other_join_recoverable(
    db,
    settings,
    client,
    parallel_course,
    monkeypatch,
):
    original = scheduler.spawn_child

    async def interrupted_spawn(session, parent, call, request, settings, **options):
        child = await original(session, parent, call, request, settings, **options)
        if request.task_type == "lesson-review":
            raise DomainError(
                "fixture_parallel_spawn_failure", "Fail after inserting the second child"
            )
        return child

    monkeypatch.setattr(scheduler, "spawn_child", interrupted_spawn)
    root = await start(client, parallel_course)
    assert await HarnessWorker(db, settings, run_ids=(UUID(root["id"]),)).run_once()
    tree = await rows(db, root["id"])
    assert len(tree) == 2 and tree[1].task_type == "lesson-practice-designer"
    parent = tree[0]
    assert parent.status == "waiting_children" and parent.children_spawned == 1
    assert parent.tools_used == parent.tree_tools_used == 3
    assert len(parent.pending_calls) == 1
    output = json.loads(parent.messages[-1]["content"])
    assert output["ok"] is False and output["error_code"] == "fixture_parallel_spawn_failure"
    monkeypatch.setattr(scheduler, "spawn_child", original)
    await drain(db, settings, root["id"])
    tree = await rows(db, root["id"])
    assert len(tree) == 4 and tree[0].status == "waiting_teacher"
    assert tree[0].children_spawned == 3
    results = [item["tool_call_id"] for item in tree[0].messages if item["role"] == "tool"]
    assert len(results) == len(set(results))


async def test_cancelling_middle_agent_cancels_only_its_subtree_and_fences_leaf(
    db,
    settings,
    client,
    parallel_course,
):
    root, designer, review, leaf = await nested(db, settings, client, parallel_course)
    worker = HarnessWorker(db, settings, run_ids=(leaf.id,))
    lease = await worker.claim()
    request, proceed = await worker.prepare_step(lease)
    assert proceed and request is not None
    await post(client, f"/v1/agent-runs/{designer.id}/cancel", {})
    tree = {item.id: item for item in await rows(db, root["id"])}
    assert tree[designer.id].status == tree[leaf.id].status == "cancelled"
    assert tree[review.id].status == "queued"
    assert tree[UUID(root["id"])].status == "waiting_children"
    with pytest.raises(LostAgentLease):
        await worker.save_reply(
            lease,
            Reply.model_validate(
                {
                    "content": "stale",
                    "metadata": {
                        "requested_model": "fixture",
                        "returned_model": "fixture",
                        "adapter_version": "fixture",
                        "usage": {},
                    },
                }
            ),
        )
    assert await HarnessWorker(db, settings, run_ids=(review.id,)).run_once()
    assert (await detail(client, review.id))["status"] == "completed"
    # Cancel the root to avoid relying on a model's policy for retrying a failed child.
    await post(client, f"/v1/agent-runs/{root['id']}/cancel", {})
    assert (await detail(client, review.id))["status"] == "completed"


async def test_failed_middle_agent_cancels_its_queued_descendants(
    db,
    settings,
    client,
    parallel_course,
):
    root, designer, review, leaf = await nested(db, settings, client, parallel_course)
    async with db.sessions.begin() as session:
        _, parent = await scheduler.lock_run(session, designer.id)
        parent.status = "running"
        parent.lease_owner = "failure-fixture"
        parent.lease_until = await session.scalar(select(func.clock_timestamp()))
        parent.epoch += 1
        # Exercise the same tree closure used after a terminal worker error.
        from teaching_assistant.harness import store

        store.finish(session, parent, "failed", "fixture_terminal_failure")
        await scheduler.cancel_descendants(session, parent, "parent_failed")
    tree = {item.id: item for item in await rows(db, root["id"])}
    assert tree[designer.id].status == "failed" and tree[leaf.id].status == "cancelled"
    assert tree[review.id].status == "queued"


async def test_parallel_queue_is_bounded_on_claim_without_losing_queued_children(
    db,
    settings,
    client,
    parallel_course,
    monkeypatch,
):
    from teaching_assistant.harness.parallel_lesson import FakeParallelLessonModel

    original = FakeParallelLessonModel.respond

    async def three_children(self, body, policy):
        reply = await original(self, body, policy)
        if len(reply.tool_calls) == 2:
            extra = reply.tool_calls[1].model_copy(deep=True)
            extra.id += "-extra"
            reply.tool_calls.append(extra)
        return reply

    monkeypatch.setattr(FakeParallelLessonModel, "respond", three_children)
    root = await start(client, parallel_course)
    assert await HarnessWorker(db, settings, run_ids=(UUID(root["id"]),)).run_once()
    tree = await rows(db, root["id"])
    assert len(tree) == 4 and all(item.status == "queued" for item in tree[1:])
    first = HarnessWorker(db, settings, run_ids=(tree[1].id,))
    second = HarnessWorker(db, settings, run_ids=(tree[2].id,))
    third = HarnessWorker(db, settings, run_ids=(tree[3].id,))
    assert await first.claim() is not None
    assert await second.claim() is not None
    assert await third.claim() is None
    await post(client, f"/v1/agent-runs/{tree[1].id}/cancel", {})
    assert await third.claim() is not None
    assert len(await rows(db, root["id"])) == 4


@pytest.mark.parametrize("resource,limit", [("model", 9), ("tool", 10)])
async def test_parallel_tree_budget_is_shared_across_both_depths(
    db,
    settings,
    client,
    parallel_course,
    resource,
    limit,
):
    root = await start(client, parallel_course, **{f"max_{resource}_calls": limit})
    await drain(db, settings, root["id"])
    tree = await rows(db, root["id"])
    assert len(tree) == 4
    assert tree[0].status == "failed"
    assert tree[0].last_error == f"agent_{resource}_budget_exhausted"
    assert sum(getattr(item, f"{resource}s_used") for item in tree) == limit
    assert all(item.status not in scheduler.ACTIVE for item in tree)


@pytest.mark.parametrize(
    "limit,error",
    [("max_depth", "agent_child_depth_exceeded"), ("max_children", "agent_child_budget_exhausted")],
)
async def test_nested_delegation_enforces_root_depth_and_total_child_limits(
    db,
    settings,
    client,
    parallel_course,
    limit,
    error,
):
    root, designer, _ = await fanout(db, settings, client, parallel_course)
    async with db.sessions.begin() as session:
        parent_root, parent = await scheduler.lock_run(session, designer.id)
        parent_root.manifest = {
            **parent_root.manifest,
            "scheduler": {**parent_root.manifest["scheduler"], limit: 1},
        }
        parent.status = "running"
        call = ToolCall.model_validate(
            {
                "id": "nested-limit",
                "function": {"name": "request_practice_agent", "arguments": "{}"},
            }
        )
        parent.pending_calls = [call.model_dump(mode="json")]
        request = ChildTaskRequest(
            "lesson-practice", dict(parent.task_input), "Fixed brief practice"
        )
        with pytest.raises(DomainError) as exc:
            await scheduler.spawn_child(session, parent, call, request, settings)
        assert exc.value.code == error
    assert len(await rows(db, root["id"])) == 3


async def test_parallel_handoff_rejects_modified_child_content(
    db, settings, client, parallel_course
):
    root = await start(client, parallel_course)
    await drain(db, settings, root["id"])
    parent = (await rows(db, root["id"]))[0]
    async with db.sessions() as session:
        artifact = await session.scalar(
            select(AgentArtifact).where(AgentArtifact.run_id == parent.id)
        )
        from teaching_assistant.harness.parallel_lesson import ParallelLessonPlan

        plan = ParallelLessonPlan.model_validate(artifact.body)
        plan.practice.prompt = "A substituted practice"
        with pytest.raises(DomainError, match="retain both received child results"):
            ParallelLessonTask.validate_body(parent, plan)
        parent.task_state["review_child"]["content_hash"] = "0" * 64
        assert not await ParallelLessonTask().artifact_exists(session, parent)


async def test_v2_parent_keeps_v2_child_profile_after_runtime_upgrade(
    db,
    settings,
    client,
    parallel_course,
):
    root = await post(
        client,
        f"/v1/courses/{parallel_course}/lesson-agent-runs",
        {
            "brief": BRIEF,
            "agent_profile": "fake-lesson-team-agent-v1",
        },
    )
    policy = catalog.v2_manifest(settings, "lesson-plan-team", "fake-lesson-team-agent-v1")
    async with db.sessions.begin() as session:
        parent = await session.get(AgentRun, UUID(root["id"]))
        parent.manifest, parent.profile_id = policy, "agent-" + digest(policy)
    assert await HarnessWorker(db, settings, run_ids=(UUID(root["id"]),)).run_once()
    tree = await rows(db, root["id"])
    assert len(tree) == 2
    assert tree[1].manifest["runtime_version"] == "harness-runtime-v2"
    assert tree[1].manifest["scheduler_version"] == "durable-children-v1"
    await drain(db, settings, root["id"])
    assert (await detail(client, root["id"]))["status"] == "waiting_teacher"


async def test_existing_v5_tree_keeps_frozen_child_budget_after_default_change(
    db, settings, client, parallel_course
):
    root = await start(client, parallel_course)
    policy = catalog.v5_manifest(settings, "lesson-plan-parallel", "fake-lesson-parallel-agent-v1")
    assert policy["scheduler"]["child_max_model_calls"] == 3
    async with db.sessions.begin() as session:
        row = await session.get(AgentRun, UUID(root["id"]))
        row.manifest, row.profile_id = policy, "agent-" + digest(policy)
    await drain(db, settings, root["id"])
    tree = await rows(db, root["id"])
    assert len(tree) == 4 and tree[0].status == "waiting_teacher"
    assert all(row.manifest["scheduler"] == policy["scheduler"] for row in tree)
    assert all(row.limits["max_model_calls"] == 3 for row in tree[1:])
    assert tree[0].tree_models_used == 10

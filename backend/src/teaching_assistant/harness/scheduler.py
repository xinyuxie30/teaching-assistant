"""Root-first locking and durable, bounded child task creation.

Every operation that mutates a task tree holds its root row before another run
row. Model HTTP calls never hold these locks. Child creation and the parent's
wait transition share the caller's transaction and savepoint.
"""

from copy import deepcopy

from sqlalchemy import func, select

from teaching_assistant.db import AgentRun
from teaching_assistant.domain import DomainError, digest
from teaching_assistant.harness import catalog, store
from teaching_assistant.harness.context import initial_messages, payload
from teaching_assistant.harness.delegation import ChildTaskRequest

ACTIVE = ("queued", "running", "waiting_children")
PARALLEL_SCHEDULER_VERSION = "durable-children-v2"


def parallel_scheduler(run):
    return run.manifest.get("scheduler_version") == PARALLEL_SCHEDULER_VERSION


async def child_slot_available(session, root, run):
    """Reserve capacity when claiming work, not when creating a queued child.

    The caller holds the root lock, so checking and moving a child to running is
    atomic across workers. Waiting ancestors and expired leases consume no slot.
    A recovering child excludes its own stale running row from the count.
    """
    if run.parent_run_id is None or not parallel_scheduler(root):
        return True
    now = await session.scalar(select(func.clock_timestamp()))
    running = await session.scalar(
        select(func.count())
        .select_from(AgentRun)
        .where(
            AgentRun.root_run_id == root.id,
            AgentRun.id != run.id,
            AgentRun.status == "running",
            AgentRun.lease_until > now,
        )
    )
    return running < root.manifest.get("scheduler", {}).get("max_parallel_children", 0)


async def lock_run(session, run_id, *, skip_locked=False):
    """Return freshly loaded (root, target), taking locks in the same order."""
    identity = (
        await session.execute(
            select(AgentRun.id, AgentRun.root_run_id).where(AgentRun.id == run_id)
        )
    ).first()
    if identity is None:
        return None, None
    root_id = identity.root_run_id or identity.id
    root = await session.scalar(
        select(AgentRun)
        .where(AgentRun.id == root_id)
        .with_for_update(skip_locked=skip_locked)
        .execution_options(populate_existing=True)
    )
    if root is None:
        return None, None
    if root.id == run_id:
        run = root
    else:
        run = await session.scalar(
            select(AgentRun)
            .where(AgentRun.id == run_id)
            .with_for_update(skip_locked=skip_locked)
            .execution_options(populate_existing=True)
        )
        if run is None:
            return None, None
    if (
        root.parent_run_id is not None
        or root.root_run_id is not None
        or (run is not root and run.root_run_id != root.id)
        or run.teacher_id != root.teacher_id
    ):
        raise DomainError("invalid_agent_tree", "The stored task tree is inconsistent")
    return root, run


def wait_for_child(session, parent, child, *, reused=False):
    parent.status = "waiting_children"
    parent.lease_owner, parent.lease_until = None, None
    parent.resume_pending = False
    store.event(
        session,
        parent,
        "waiting_children",
        child_run_id=str(child.id),
        tool_call_id=child.parent_tool_call_id,
        reused=reused,
    )


def wait_for_children(session, parent):
    """Release the parent lease after a group of independent child dispatches."""
    parent.status = "waiting_children"
    parent.lease_owner, parent.lease_until = None, None
    parent.resume_pending = False
    store.event(
        session,
        parent,
        "waiting_children",
        tool_call_ids=[call["id"] for call in parent.pending_calls],
    )


async def spawn_child(session, parent, call, request, settings, *, defer_wait=False):
    """Spawn one child under the already locked root, or reuse its saved identity."""
    if not isinstance(request, ChildTaskRequest):
        raise DomainError("invalid_child_request", "Only registered code can request a child")
    root = await session.get(AgentRun, parent.root_run_id or parent.id)
    if root is None or parent.teacher_id != root.teacher_id:
        raise DomainError("invalid_agent_tree", "The stored task tree is inconsistent")
    if parent.status != "running" or root.status not in ACTIVE:
        raise DomainError("agent_not_running", "The parent task can no longer delegate")
    if defer_wait and not parallel_scheduler(root):
        raise DomainError("invalid_child_request", "This profile cannot dispatch parallel children")
    pending_ids = [item["id"] for item in parent.pending_calls]
    if not pending_ids or call.id not in (pending_ids if defer_wait else pending_ids[:1]):
        raise DomainError("invalid_child_request", "A child requires the current pending tool call")
    permitted = catalog.ALLOWED_CHILDREN.get(parent.task_type, ())
    if request.task_type not in permitted or request.task_type not in parent.manifest.get(
        "allowed_children", []
    ):
        raise DomainError("agent_child_task_not_allowed", "This task cannot delegate that task")
    inherited_input = deepcopy(parent.task_input)
    if (
        not isinstance(inherited_input, dict)
        or not isinstance(request.task_input, dict)
        or request.task_input != inherited_input
    ):
        raise DomainError("invalid_child_scope", "Child input must preserve the parent's scope")
    if (
        not isinstance(request.instructions, str)
        or not request.instructions.strip()
        or len(request.instructions) > 2000
    ):
        raise DomainError(
            "invalid_child_request", "Child instructions must be bounded and nonempty"
        )

    existing = await session.scalar(
        select(AgentRun).where(
            AgentRun.parent_run_id == parent.id, AgentRun.parent_tool_call_id == call.id
        )
    )
    if existing is not None:
        if (
            existing.root_run_id != root.id
            or existing.teacher_id != parent.teacher_id
            or existing.task_type != request.task_type
            or existing.task_input != inherited_input
        ):
            raise DomainError("invalid_agent_tree", "The saved child does not match its parent")
        if not defer_wait:
            wait_for_child(session, parent, existing, reused=True)
        return existing

    policy = root.manifest.get("scheduler", {})
    if parent.depth + 1 > policy.get("max_depth", 0):
        raise DomainError("agent_child_depth_exceeded", "The task tree depth limit is exhausted")
    if root.children_spawned >= policy.get("max_children", 0):
        raise DomainError("agent_child_budget_exhausted", "The task tree child limit is exhausted")
    if not parallel_scheduler(root):
        active_count = await session.scalar(
            select(func.count())
            .select_from(AgentRun)
            .where(AgentRun.root_run_id == root.id, AgentRun.status.in_(ACTIVE))
        )
        if active_count >= policy.get("max_parallel_children", 0):
            raise DomainError(
                "agent_child_concurrency_limit", "The task tree already has an active child"
            )
    now = await session.scalar(select(func.clock_timestamp()))
    deadline = min(parent.expires_at, root.expires_at)
    if deadline <= now:
        raise DomainError("agent_deadline_exceeded", "The task tree deadline has elapsed")

    provider = parent.manifest["provider"]
    selected = next(
        (
            item
            for item in catalog.profiles(settings, task_type=request.task_type, include_legacy=True)
            if item["alias"].startswith(provider + "-")
            and item["manifest"].get("runtime_version") == parent.manifest.get("runtime_version")
            and item["manifest"].get("scheduler") == parent.manifest.get("scheduler")
        ),
        None,
    )
    if selected is None or not selected["ready"]:
        raise DomainError("model_not_configured", "The child model profile is not available")
    task = catalog.resolve(request.task_type)
    limits = {
        **deepcopy(parent.limits),
        "max_model_calls": min(
            parent.limits["max_model_calls"],
            root.limits["max_model_calls"],
            policy["child_max_model_calls"],
        ),
        "max_tool_calls": min(
            parent.limits["max_tool_calls"],
            root.limits["max_tool_calls"],
            policy["child_max_tool_calls"],
        ),
        "max_context_characters": min(
            parent.limits["max_context_characters"], root.limits["max_context_characters"]
        ),
        "max_seconds": max(1, int((deadline - now).total_seconds())),
    }
    messages = initial_messages(task.system, request.instructions)
    payload(messages, selected["manifest"], limits, task.registry.schemas())
    child = AgentRun(
        teacher_id=parent.teacher_id,
        task_type=request.task_type,
        task_input=deepcopy(inherited_input),
        parent_run_id=parent.id,
        root_run_id=root.id,
        parent_tool_call_id=call.id,
        depth=parent.depth + 1,
        profile_id=selected["id"],
        manifest=deepcopy(selected["manifest"]),
        limits=limits,
        messages=messages,
        expires_at=deadline,
    )
    await task.authorize(session, child)
    session.add(child)
    await session.flush()
    root.children_spawned += 1
    store.event(
        session,
        child,
        "created",
        task_type=child.task_type,
        profile_id=child.profile_id,
        parent_run_id=str(parent.id),
        root_run_id=str(root.id),
        parent_tool_call_id=call.id,
        input_hash=digest(child.task_input),
    )
    store.event(
        session,
        parent,
        "child_spawned",
        child_run_id=str(child.id),
        task_type=child.task_type,
        tool_call_id=call.id,
    )
    if not defer_wait:
        wait_for_child(session, parent, child)
    return child


async def cancel_descendants(session, run, code):
    """Cancel active descendants while retaining every saved result and receipt."""
    # Include terminal intermediate rows while finding descendants: their already
    # queued descendants must not survive an ancestor cancellation or failure.
    tree_rows = list(
        await session.scalars(
            select(AgentRun)
            .where(AgentRun.root_run_id == (run.root_run_id or run.id))
            .order_by(AgentRun.depth, AgentRun.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    descendants = {run.id}
    for child in tree_rows:
        if child.parent_run_id in descendants:
            descendants.add(child.id)
            if child.status in (*ACTIVE, "waiting_input", "waiting_timer"):
                store.finish(session, child, "cancelled", code)
                child.epoch += 1
                child.resume_pending = False
                if hasattr(child, "wait_state"):
                    child.wait_state = {}
                if hasattr(child, "pending_user_messages"):
                    child.pending_user_messages = []


async def terminate_tree(session, root, code):
    """Close a tree atomically after a root budget or deadline failure."""
    if root.status in ACTIVE:
        store.finish(session, root, "failed", code)
        root.epoch += 1
        root.resume_pending = False
    await cancel_descendants(session, root, code)

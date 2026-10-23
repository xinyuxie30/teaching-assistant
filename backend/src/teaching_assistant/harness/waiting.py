"""Root-scoped durable waits. External waiting does not spend execution time or model calls."""

from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select

from teaching_assistant.db import AgentRun, AgentWait, AgentWake
from teaching_assistant.domain import DomainError, digest
from teaching_assistant.harness import scheduler
from teaching_assistant.harness.store import close_inflight, event
from teaching_assistant.schemas import json_time

WAIT_VERSION = "durable-waits-v1"
WAITING = ("waiting_input", "waiting_timer")


def require_supported(root):
    if root.parent_run_id is not None:
        raise DomainError(
            "agent_root_required", "Pause and wake apply to a complete root task tree", 409
        )
    if root.manifest.get("waiting_version") != WAIT_VERSION:
        raise DomainError(
            "agent_wait_unsupported", "This pinned runtime does not support waits", 409
        )


async def tree_rows(session, root):
    # The caller holds the root before locking descendants in a deterministic order.
    return [
        root,
        *list(
            await session.scalars(
                select(AgentRun)
                .where(AgentRun.root_run_id == root.id)
                .order_by(AgentRun.depth, AgentRun.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ),
    ]


async def pause_tree(session, root, *, reason, wake_at=None, question=None):
    require_supported(root)
    if question is not None:
        from teaching_assistant.harness.questions import QuestionSnapshot
        from teaching_assistant.harness.questions import require_supported as require_questions

        require_questions(root)
        question = QuestionSnapshot.model_validate(question).model_dump(mode="json")
        if wake_at is not None:
            raise DomainError(
                "agent_question_timer_forbidden", "A teacher question cannot auto-wake"
            )
    if root.status not in scheduler.ACTIVE:
        raise DomainError("agent_not_waitable", "Only an active root task can be paused", 409)
    now = await session.scalar(select(func.clock_timestamp()))
    if root.expires_at <= now:
        raise DomainError("agent_deadline_exceeded", "An expired execution cannot be paused", 409)
    if wake_at is not None and wake_at <= now:
        raise DomainError("invalid_wake_time", "Timer wake time must be in the future", 422)
    rows = [row for row in await tree_rows(session, root) if row.status in scheduler.ACTIVE]
    token = uuid4()
    status = "waiting_timer" if wake_at else "waiting_input"
    snapshot = {
        "version": WAIT_VERSION,
        "paused_at": json_time(now),
        "nodes": [
            {
                "id": str(row.id),
                "status": row.status,
                "remaining_seconds": max(0, (row.expires_at - now).total_seconds()),
                "ready_delay_seconds": max(0, (row.ready_at - now).total_seconds()),
                "resume_pending": row.resume_pending or row.status == "running",
            }
            for row in rows
        ],
        **({"question": question} if question is not None else {}),
    }
    session.add(
        AgentWait(
            id=token,
            run_id=root.id,
            kind=status,
            reason=reason,
            wake_at=wake_at,
            snapshot=snapshot,
            content_hash=digest(snapshot),
        )
    )
    for row in rows:
        close_inflight(session, row, "teacher_paused")
        row.epoch += 1
        row.status = status
        row.lease_owner = row.lease_until = None
        row.wait_state = {
            "token": str(token),
            "root_run_id": str(root.id),
            "reason": reason,
            "wake_at": json_time(wake_at),
            **({"question": question} if question is not None and row.id == root.id else {}),
        }
        event(session, row, "paused", wait_token=str(token), waiting_kind=status, reason=reason)
    return token


async def wake_tree(session, root, wait_token, *, response=None, source="teacher"):
    require_supported(root)
    saved = await session.get(AgentWait, wait_token)
    if saved is None or saved.run_id != root.id:
        raise DomainError("agent_wait_not_found", "Wait token does not belong to this root", 404)
    request_hash = digest({"wait_token": str(wait_token), "source": source, "response": response})
    receipt = await session.scalar(select(AgentWake).where(AgentWake.wait_id == wait_token))
    if receipt is not None:
        if receipt.request_hash != request_hash:
            raise DomainError(
                "agent_wait_already_resolved", "This wait already has a different receipt", 409
            )
        return False
    if root.status not in WAITING or root.wait_state.get("token") != str(wait_token):
        raise DomainError("agent_wait_stale", "This token is not the root's current wait", 409)
    if (
        digest(saved.snapshot) != saved.content_hash
        or saved.snapshot.get("version") != WAIT_VERSION
    ):
        raise DomainError("agent_wait_corrupt", "Saved wait snapshot failed verification", 409)
    question = saved.snapshot.get("question")
    if question is not None:
        from teaching_assistant.harness.contracts import ToolCall
        from teaching_assistant.harness.questions import require_supported as require_questions
        from teaching_assistant.harness.questions import validate_snapshot

        require_questions(root)
        if source != "teacher":
            raise DomainError(
                "agent_question_timer_forbidden", "Only a teacher may answer a question"
            )
        if not isinstance(response, str) or not response.strip() or len(response) > 4000:
            raise DomainError("agent_answer_required", "Answer the pending teacher question", 422)
        if not root.pending_calls:
            raise DomainError("agent_question_corrupt", "The pending question call is missing", 409)
        validate_snapshot(saved, ToolCall.model_validate(root.pending_calls[0]))
    now = await session.scalar(select(func.clock_timestamp()))
    if source == "timer" and (saved.wake_at is None or saved.wake_at > now):
        return False
    rows = {str(row.id): row for row in await tree_rows(session, root)}
    nodes = saved.snapshot["nodes"]
    for node in nodes:
        row = rows.get(node["id"])
        if (
            row is None
            or row.teacher_id != root.teacher_id
            or row.status not in WAITING
            or row.wait_state.get("token") != str(wait_token)
        ):
            raise DomainError(
                "agent_wait_tree_changed", "Paused task tree failed verification", 409
            )
    for node in nodes:
        row = rows[node["id"]]
        row.status = "queued" if node["status"] == "running" else node["status"]
        row.expires_at = now + timedelta(seconds=node["remaining_seconds"])
        row.ready_at = now + timedelta(seconds=node["ready_delay_seconds"])
        row.resume_pending = node["resume_pending"]
        row.wait_state = {}
        event(session, row, "woken", wait_token=str(wait_token), source=source)
    if response is not None and question is None:
        root.pending_user_messages = [
            *root.pending_user_messages,
            {"role": "user", "content": response},
        ]
    session.add(
        AgentWake(wait_id=wait_token, source=source, response=response, request_hash=request_hash)
    )
    return True


async def cancel_wait(session, root):
    """Close a paused root before generic cancellation; cancelled waits cannot be scanned again."""
    if root.status not in WAITING:
        return
    require_supported(root)
    token = UUID(root.wait_state["token"])
    if await session.scalar(select(AgentWake).where(AgentWake.wait_id == token)) is None:
        session.add(
            AgentWake(
                wait_id=token,
                source="cancelled",
                response=None,
                request_hash=digest({"wait_token": str(token), "source": "cancelled"}),
            )
        )
    for row in await tree_rows(session, root):
        if row.wait_state.get("token") == str(token):
            row.wait_state = {}
            row.pending_user_messages = []


def apply_pending_input(session, run):
    """Inject teacher responses only after all paired tool results have been persisted."""
    if run.pending_calls or not run.pending_user_messages:
        return
    run.messages = [*run.messages, *run.pending_user_messages]
    event(session, run, "wake_input_applied", message_count=len(run.pending_user_messages))
    run.pending_user_messages = []


async def scan_due_waits(db, *, run_ids=None, limit=100):
    """A bounded structural heartbeat. Concurrent scans share root locks and unique receipts."""
    query = (
        select(AgentWait.id, AgentRun)
        .join(AgentRun, AgentWait.run_id == AgentRun.id)
        .where(
            AgentRun.status == "waiting_timer",
            AgentRun.parent_run_id.is_(None),
            AgentWait.wake_at <= func.clock_timestamp(),
            ~select(AgentWake.id).where(AgentWake.wait_id == AgentWait.id).exists(),
        )
        .order_by(AgentWait.wake_at, AgentWait.id)
        # Skip contended roots in SQL before LIMIT, so one busy prefix cannot
        # starve later timers. Lock no wait/child row before its root.
        .with_for_update(of=AgentRun, skip_locked=True)
        .limit(1)
    )
    if run_ids is not None:
        selected_roots = (
            select(func.coalesce(AgentRun.root_run_id, AgentRun.id))
            .where(AgentRun.id.in_(run_ids))
            .correlate(None)
        )
        query = query.where(AgentWait.run_id.in_(selected_roots))
    count = 0
    seen = []
    for _ in range(limit):
        async with db.sessions.begin() as session:
            candidate = (await session.execute(query.where(AgentWait.id.not_in(seen)))).first()
            if candidate is None:
                break
            token, root = candidate
            seen.append(token)
            if root.status != "waiting_timer" or root.wait_state.get("token") != str(token):
                continue
            count += bool(await wake_tree(session, root, token, source="timer"))
    return count

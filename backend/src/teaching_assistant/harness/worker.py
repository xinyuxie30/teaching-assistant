"""Durable model/tool loop with fenced, atomic tool effects and deny-only hooks."""

import asyncio
import logging
from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import String, and_, cast, func, inspect, or_, select, tuple_
from sqlalchemy.orm import aliased

from teaching_assistant.db import AgentArtifact, AgentEvent, AgentRun
from teaching_assistant.domain import DomainError, ModelError, digest
from teaching_assistant.harness import (
    background,
    budgets,
    catalog,
    hooks,
    questions,
    scheduler,
    tool_results,
    waiting,
)
from teaching_assistant.harness import store as service
from teaching_assistant.harness.context import tool_result
from teaching_assistant.harness.context_store import accept_summary, prepare_context
from teaching_assistant.harness.contracts import Reply, ToolCall
from teaching_assistant.harness.delegation import ChildTaskRequest
from teaching_assistant.harness.models import build_model
from teaching_assistant.harness.semantic_compaction import SummaryRequest
from teaching_assistant.worker_backoff import failure_delay


class LostAgentLease(Exception):
    pass


@dataclass(frozen=True)
class AgentLease:
    run_id: UUID
    epoch: int
    owner: str


class HarnessWorker:
    candidate_page_size = 100

    def __init__(self, db, settings, *, run_ids=None, hooks=None, available_profiles=None):
        self.db, self.settings, self.run_ids = db, settings, run_ids
        self.owner = "agent-" + uuid4().hex
        self.hooks = hooks
        self.override_hooks = hooks
        self.available = (
            deepcopy(available_profiles)
            if available_profiles is not None
            else {
                p["id"]: p["manifest"]
                for p in catalog.profiles(settings, include_legacy=True)
                if p["ready"]
            }
        )

    def build_model(self, policy):
        return build_model(self.settings, policy)

    async def candidate_ids(self):
        """Bound candidate memory without starving work behind locked/waiting rows."""
        async with self.db.sessions() as session:
            now = await session.scalar(select(func.clock_timestamp()))
        child = aliased(AgentRun)
        first_call = AgentRun.pending_calls[0]["id"].astext
        join = AgentRun.task_state["background_joins"][first_call]
        # Do not take root locks just to rediscover that a dependency is still
        # running. Match only the first pending receipt (or its explicit join),
        # so unrelated background work cannot delay a ready handoff. Missing
        # dependencies remain candidates for the locked reducer to fail.
        sleeping = (
            select(child.id)
            .where(
                child.parent_run_id == AgentRun.id,
                or_(
                    and_(
                        child.parent_tool_call_id == first_call,
                        child.status.in_(scheduler.ACTIVE),
                    ),
                    and_(
                        cast(child.id, String) == join["child_run_id"].astext,
                        child.parent_tool_call_id == join["spawn_tool_call_id"].astext,
                        child.status.in_((*scheduler.ACTIVE, "waiting_input", "waiting_timer")),
                    ),
                ),
            )
            .exists()
        )
        query = select(AgentRun.id, AgentRun.ready_at).where(
            AgentRun.created_at <= now,
            or_(AgentRun.profile_id.in_(self.available), AgentRun.expires_at <= now),
            AgentRun.status.in_(scheduler.ACTIVE),
            or_(
                AgentRun.expires_at <= now,
                and_(AgentRun.status == "waiting_children", ~sleeping),
                and_(AgentRun.status == "queued", AgentRun.ready_at <= now),
                and_(AgentRun.status == "running", AgentRun.lease_until <= now),
            ),
        )
        if self.run_ids is not None:
            query = query.where(
                or_(AgentRun.id.in_(self.run_ids), AgentRun.root_run_id.in_(self.run_ids))
            )
        cursor = None
        while True:
            page = query
            if cursor is not None:
                page = page.where(tuple_(AgentRun.ready_at, AgentRun.id) > cursor)
            async with self.db.sessions() as session:
                rows = (
                    await session.execute(
                        page.order_by(AgentRun.ready_at, AgentRun.id).limit(
                            self.candidate_page_size
                        )
                    )
                ).all()
            # Release the read connection before any root-first locking or work.
            for row in rows:
                yield row.id
            if len(rows) < self.candidate_page_size:
                return
            cursor = (rows[-1].ready_at, rows[-1].id)

    async def claim(self):
        await waiting.scan_due_waits(self.db, run_ids=self.run_ids)
        # Read candidates without locking a child first. Every mutation locks the
        # root before its target, including cancellation and shared-budget charging.
        async for run_id in self.candidate_ids():
            async with self.db.sessions.begin() as session:
                root, run = await scheduler.lock_run(session, run_id, skip_locked=True)
                if run is None or run.status not in scheduler.ACTIVE:
                    continue
                now = await session.scalar(select(func.clock_timestamp()))
                if root.expires_at <= now:
                    await scheduler.terminate_tree(session, root, "agent_deadline_exceeded")
                    continue
                if root.status not in scheduler.ACTIVE:
                    service.finish(session, run, "cancelled", "parent_not_active")
                    run.epoch += 1
                    continue
                if run.expires_at <= now:
                    service.finish(session, run, "failed", "agent_deadline_exceeded")
                    await scheduler.cancel_descendants(session, run, "parent_failed")
                    continue
                if (
                    run.status == "running"
                    and run.lease_until is not None
                    and run.lease_until > now
                ) or (run.status == "queued" and run.ready_at > now):
                    continue
                if (
                    run.manifest != self.available.get(run.profile_id)
                    or run.manifest.get("task_type", "rubric") != run.task_type
                ):
                    service.finish(session, run, "failed", "agent_profile_mismatch")
                    await scheduler.cancel_descendants(session, run, "parent_failed")
                    continue
                try:
                    catalog.resolve(run.task_type)
                    self.restore_hooks(run)
                except DomainError:
                    service.finish(
                        session,
                        run,
                        "failed",
                        "agent_hook_mismatch"
                        if run.manifest.get("hook_snapshot")
                        else "agent_task_not_supported",
                    )
                    await scheduler.cancel_descendants(session, run, "parent_failed")
                    continue
                if run.status == "waiting_children" and not await self.resume_parent(session, run):
                    continue
                if not await scheduler.child_slot_available(session, root, run):
                    continue
                # Receiving child results may involve bounded reducers and database
                # waits. Recheck deadlines and date the new lease after that work.
                now = await session.scalar(select(func.clock_timestamp()))
                if root.expires_at <= now:
                    await scheduler.terminate_tree(session, root, "agent_deadline_exceeded")
                    continue
                if run.expires_at <= now:
                    service.finish(session, run, "failed", "agent_deadline_exceeded")
                    await scheduler.cancel_descendants(session, run, "parent_failed")
                    continue
                if run.attempts >= 3 and not run.resume_pending:
                    service.finish(session, run, "failed", "agent_attempts_exhausted")
                    await scheduler.cancel_descendants(session, run, "parent_failed")
                    continue
                service.close_inflight(session, run, "lease_expired")
                run.epoch += 1
                if not run.resume_pending:
                    run.attempts += 1
                run.resume_pending = False
                run.status, run.last_error = "running", None
                run.lease_owner, run.lease_until = (
                    self.owner,
                    now + timedelta(seconds=self.settings.lease_seconds),
                )
                service.event(session, run, "claimed", attempt=run.attempts)
                return AgentLease(run.id, run.epoch, self.owner)
        return None

    async def resume_parent(self, session, run):
        """Drain ordered child receipts, keeping the parent asleep until its join completes."""
        if background.is_join_wait(run):
            try:
                ready = await background.pending_join(session, run)
            except DomainError as exc:
                ready = (
                    ToolCall.model_validate(run.pending_calls[0]),
                    {"ok": False, "error_code": exc.code},
                )
            if ready is None:
                return False
            self.restore_hooks(run)
            await self.receive_join(session, run, *ready)
            return True
        if not run.pending_calls:
            service.finish(session, run, "failed", "agent_child_missing")
            await scheduler.cancel_descendants(session, run, "parent_failed")
            return False
        while run.pending_calls:
            call = ToolCall.model_validate(run.pending_calls[0])
            child = await session.scalar(
                select(AgentRun).where(
                    AgentRun.parent_run_id == run.id, AgentRun.parent_tool_call_id == call.id
                )
            )
            if child is None:
                # A serial/mixed reply can still contain undispatched tool calls.
                if run.status == "queued":
                    return True
                service.finish(session, run, "failed", "agent_child_missing")
                await scheduler.cancel_descendants(session, run, "parent_failed")
                return False
            if child.status in scheduler.ACTIVE:
                run.status = "waiting_children"
                return False
            await self.receive_child(session, run, call, child)
        return True

    async def receive_child(self, session, run, call, child):
        """Deliver the child receipt once, under the same root lock as cancellation."""
        task = catalog.resolve(run.task_type)
        try:
            await task.authorize(session, run)
            if child.status != "completed":
                raise DomainError("agent_child_failed", "The delegated task did not complete")
            if (
                child.teacher_id != run.teacher_id
                or child.root_run_id != (run.root_run_id or run.id)
                or child.task_type not in run.manifest.get("allowed_children", [])
                or child.task_input != run.task_input
            ):
                raise DomainError("invalid_child_result", "Child scope does not match the parent")
            child_task = catalog.resolve(child.task_type)
            await child_task.authorize(session, child)
            artifact = await session.scalar(
                select(AgentArtifact).where(AgentArtifact.run_id == child.id)
            )
            if (
                artifact is None
                or artifact.kind != child.task_type
                or artifact.confirmed_at is not None
                or artifact.content_hash != digest(artifact.body)
                or not await child_task.artifact_exists(session, child)
            ):
                raise DomainError("invalid_child_result", "Child has no valid unconfirmed artifact")
            async with session.begin_nested():
                async with asyncio.timeout(task.registry.get(call.function.name).timeout_seconds):
                    result = task.registry.validate_result(
                        call.function.name,
                        {
                            "child_run_id": str(child.id),
                            "artifact_id": str(artifact.id),
                            "content_hash": artifact.content_hash,
                            "task_type": child.task_type,
                            "body": artifact.body,
                            "confirmed": False,
                        },
                    )
                    self.post_hook(call.function.name, result)
                    await task.apply_result(session, run, call.function.name, result)
            output = {"ok": True, "result": result}
        except (DomainError, TimeoutError) as exc:
            if inspect(run).expired_attributes:
                await session.refresh(run)
            output = {
                "ok": False,
                "error_code": exc.code if isinstance(exc, DomainError) else "tool_timeout",
            }
        service.event(
            session, run, "post_tool", tool_call_id=call.id, name=call.function.name, output=output
        )
        service.event(
            session,
            run,
            "child_received",
            child_run_id=str(child.id),
            tool_call_id=call.id,
            status=child.status,
        )
        run.messages = [*run.messages, tool_result(call.id, output)]
        run.pending_calls = run.pending_calls[1:]
        run.status, run.resume_pending = "queued", True
        run.ready_at = await session.scalar(select(func.clock_timestamp()))
        return True

    def post_hook(self, name, result):
        if self.hooks is not None:
            result_hash = digest(result)
            self.hooks.post_tool(name, result)
            if digest(result) != result_hash:
                raise DomainError(
                    "invalid_tool_result", "Hooks cannot change validated tool results"
                )

    def restore_hooks(self, run):
        saved = run.manifest.get("hook_snapshot")
        if saved is None:
            self.hooks = self.override_hooks
        elif self.override_hooks is not None:
            raise ModelError("agent_hook_mismatch", retryable=False)
        else:
            self.hooks = hooks.build(saved)

    async def fenced(self, session, lease):
        root, run = await scheduler.lock_run(session, lease.run_id)
        now = await session.scalar(select(func.clock_timestamp()))
        if (
            run is None
            or root.status not in scheduler.ACTIVE
            or run.status != "running"
            or run.epoch != lease.epoch
            or run.lease_owner != lease.owner
            or run.lease_until is None
            or run.lease_until <= now
        ):
            raise LostAgentLease()
        if run.expires_at <= now or root.expires_at <= now:
            raise ModelError("agent_deadline_exceeded", retryable=False)
        run.lease_until = now + timedelta(seconds=self.settings.lease_seconds)
        return run

    async def budget_available(self, session, run, resource):
        root = await session.get(AgentRun, run.root_run_id or run.id)
        counter = f"tree_{resource}s_used"
        # Compatibility for migrated or explicitly seeded pre-tree root runs.
        used = max(getattr(root, counter), getattr(root, f"{resource}s_used"))
        setattr(root, counter, used)
        if used >= root.limits[f"max_{resource}_calls"]:
            await scheduler.terminate_tree(session, root, f"agent_{resource}_budget_exhausted")
            return False
        return True

    async def finish_step(self, session, run):
        """Check elapsed work before committing effects under the already held root lock."""
        root = await session.get(AgentRun, run.root_run_id or run.id)
        await session.flush()
        now = await session.scalar(select(func.clock_timestamp()))
        # A durable question has already frozen the tree's execution clock.
        if root.status not in waiting.WAITING:
            if min(run.expires_at, root.expires_at) <= now:
                raise ModelError("agent_deadline_exceeded", retryable=False)
            if run.status == "running":
                run.lease_until = now + timedelta(seconds=self.settings.lease_seconds)

    async def tool_step(self, session, run, *, call=None, defer_wait=False):
        task_definition = catalog.resolve(run.task_type)
        call = call or ToolCall.model_validate(run.pending_calls[0])
        if not await self.budget_available(session, run, "tool"):
            return
        if run.tools_used >= run.limits["max_tool_calls"]:
            service.finish(session, run, "failed", "agent_tool_budget_exhausted")
            await scheduler.cancel_descendants(session, run, "parent_failed")
            return
        run.tools_used += 1
        root = await session.get(AgentRun, run.root_run_id or run.id)
        root.tree_tools_used += 1
        name = call.function.name
        try:
            arguments = task_definition.registry.validate_call(call)
            await task_definition.authorize(session, run)
            blocked = task_definition.pre_tool(run, name)
            if blocked is None and self.hooks is not None:
                blocked = self.hooks.pre_tool(name, task_definition.hook_context(run))
            service.event(
                session,
                run,
                "pre_tool",
                tool_call_id=call.id,
                name=name,
                arguments_hash=digest(call.function.arguments),
                blocked=blocked,
            )
            if blocked:
                raise DomainError(blocked, "Tool blocked by task preconditions")
            tool = task_definition.registry.get(name)
            async with session.begin_nested():
                async with asyncio.timeout(tool.timeout_seconds):
                    raw_result = await tool.handler(session, run, arguments)
                    if isinstance(raw_result, background.BackgroundChildRequest):
                        if tool.effect != "background_spawn":
                            raise DomainError(
                                "tool_not_allowed", "This tool cannot spawn background tasks"
                            )
                        raw_result = await background.spawn_child(
                            session, run, call, raw_result, self.settings
                        )
                    if isinstance(raw_result, background.ChildJoinRequest):
                        if tool.effect != "child_join":
                            raise DomainError(
                                "tool_not_allowed", "This tool cannot join a background task"
                            )
                        raw_result = await background.begin_join(session, run, call, raw_result)
                        if raw_result is None:
                            return
                    if isinstance(raw_result, questions.TeacherInputRequest):
                        if tool.effect != "ask":
                            raise DomainError("tool_not_allowed", "This tool cannot request input")
                        await questions.begin_question(session, run, call, raw_result)
                        return
                    if isinstance(raw_result, ChildTaskRequest):
                        if tool.effect != "delegate":
                            raise DomainError("tool_not_allowed", "This tool cannot delegate")
                        await scheduler.spawn_child(
                            session, run, call, raw_result, self.settings, defer_wait=defer_wait
                        )
                        return  # The same call receives its result only after child termination.
                    result = task_definition.registry.validate_result(name, raw_result)
                    self.post_hook(name, result)
                    await task_definition.apply_result(session, run, name, result)
                    await tool_results.persist_receipt(session, run, call, result)
            output = {"ok": True, "result": result}
        except DomainError as exc:
            # A rollback expires state modified by a handler or reducer. Reload explicitly
            # before event bookkeeping; implicit expired-attribute IO is invalid with asyncio.
            # Refresh only expired state so pre-validation errors keep unflushed budgets.
            if inspect(run).expired_attributes:
                await session.refresh(run)
            if inspect(root).expired_attributes:
                await session.refresh(root)
            output = {"ok": False, "error_code": exc.code, "detail": exc.detail}
        except TimeoutError:
            if inspect(run).expired_attributes:
                await session.refresh(run)
            if inspect(root).expired_attributes:
                await session.refresh(root)
            output = {
                "ok": False,
                "error_code": "tool_timeout",
                "detail": "Tool time limit exceeded",
            }
        service.event(session, run, "post_tool", tool_call_id=call.id, name=name, output=output)
        run.messages = [*run.messages, tool_result(call.id, output)]
        run.pending_calls = [pending for pending in run.pending_calls if pending["id"] != call.id]
        # Artifact save + state + tool receipt + waiting transition are atomic.
        if await task_definition.artifact_exists(session, run):
            accepted = self.hooks is None or self.hooks.stop(task_definition.hook_context(run))
            service.event(
                session,
                run,
                "stop_hook",
                accepted=accepted,
                reason="artifact_saved",
                waiting_for=task_definition.waiting_for,
            )
            service.finish(
                session,
                run,
                getattr(task_definition, "completion_status", "waiting_teacher")
                if accepted
                else "failed",
                None if accepted else "agent_stop_blocked",
            )
            if not accepted:
                await scheduler.cancel_descendants(session, run, "parent_failed")

    async def prepare_step(self, lease):
        async with self.db.sessions.begin() as session:
            run = await self.fenced(session, lease)
            self.restore_hooks(run)
            task_definition = catalog.resolve(run.task_type)
            # Code-owned task dependencies can yield without polling through a
            # paid model or spending an execution attempt. The normal deadline,
            # ownership checks and fenced transaction still apply.
            ready = getattr(task_definition, "ready_to_continue", None)
            if ready is not None:
                await task_definition.authorize(session, run)
                if not await ready(session, run):
                    await self.finish_step(session, run)
                    return None, False
            answered = await questions.pending_answer(session, run)
            if answered is not None:
                await self.receive_answer(session, run, *answered)
                await self.finish_step(session, run)
                return None, True
            if run.pending_calls and run.pending_user_messages:
                # A resumed teacher response can invalidate an already proposed effect.
                # Pair the unexecuted calls before injecting input; never save an old
                # proposal and silently leave the new instructions behind at stop.
                try:
                    first_tool = task_definition.registry.get(
                        ToolCall.model_validate(run.pending_calls[0]).function.name
                    )
                except DomainError:
                    first_tool = None
                if first_tool is not None and first_tool.effect in {
                    "draft_write",
                    "delegate",
                    "ask",
                    "background_spawn",
                }:
                    service.close_pending(session, run, "teacher_input_changed")
            if run.pending_calls:
                calls = [ToolCall.model_validate(call) for call in run.pending_calls]
                # Only a code-registered, all-delegation group is dispatched together.
                # Mixed read/write groups keep their original ordered execution.
                parallel = run.manifest.get("scheduler_version") == "durable-children-v2"
                if parallel:
                    try:
                        parallel = all(
                            task_definition.registry.get(call.function.name).effect == "delegate"
                            for call in calls
                        )
                    except DomainError:
                        parallel = False
                if parallel:
                    for call in calls:
                        await self.tool_step(session, run, call=call, defer_wait=True)
                        if run.status != "running":
                            break
                    if run.status == "running" and run.pending_calls:
                        scheduler.wait_for_children(session, run)
                else:
                    await self.tool_step(session, run)
                await self.finish_step(session, run)
                return None, run.status == "running"
            waiting.apply_pending_input(session, run)
            for resource in ("model", "tool"):
                if not await self.budget_available(session, run, resource):
                    return None, False
            if run.models_used >= run.limits["max_model_calls"]:
                service.finish(session, run, "failed", "agent_model_budget_exhausted")
                await scheduler.cancel_descendants(session, run, "parent_failed")
                return None, False
            if run.tools_used >= run.limits["max_tool_calls"]:
                service.finish(session, run, "failed", "agent_tool_budget_exhausted")
                await scheduler.cancel_descendants(session, run, "parent_failed")
                return None, False
            task_definition = catalog.resolve(run.task_type)
            await task_definition.authorize(session, run)
            context = await prepare_context(session, run, task_definition)
            summary_plan = context.plan if isinstance(context, SummaryRequest) else None
            body = context.body if isinstance(context, SummaryRequest) else context
            root = await session.get(AgentRun, run.root_run_id or run.id)
            now = await session.scalar(select(func.clock_timestamp()))
            if min(run.expires_at, root.expires_at) <= now:
                raise ModelError("agent_deadline_exceeded", retryable=False)
            run.models_used += 1
            root.tree_models_used += 1
            sequence = service.event(
                session,
                run,
                "model_dispatched",
                prompt_hash=digest(body),
                requested_model=run.manifest["model"],
                ordinal=run.models_used,
                purpose="context_summary" if summary_plan is not None else "task",
                **({"summary_plan": summary_plan} if summary_plan is not None else {}),
            )
            await budgets.reserve(session, run, sequence, body)
            run.inflight_seq = sequence
            await session.flush()
            now = await session.scalar(select(func.clock_timestamp()))
            remaining = (min(run.expires_at, root.expires_at) - now).total_seconds()
            if remaining <= 0:
                raise ModelError("agent_deadline_exceeded", retryable=False)
            run.lease_until = now + timedelta(seconds=self.settings.lease_seconds)
            timeout_seconds = min(run.manifest["call_timeout_seconds"], remaining)
            return (body, run.manifest, timeout_seconds), True

    async def receive_answer(self, session, run, call, answer):
        """Pair a durable teacher answer once, without charging a second tool call."""
        task = catalog.resolve(run.task_type)
        try:
            await task.authorize(session, run)
            async with session.begin_nested():
                async with asyncio.timeout(task.registry.get(call.function.name).timeout_seconds):
                    result = task.registry.validate_result(call.function.name, answer["result"])
                    self.post_hook(call.function.name, result)
                    await task.apply_result(session, run, call.function.name, result)
            output = {"ok": True, "result": result}
        except (DomainError, TimeoutError) as exc:
            if inspect(run).expired_attributes:
                await session.refresh(run)
            output = {
                "ok": False,
                "error_code": exc.code if isinstance(exc, DomainError) else "tool_timeout",
            }
        service.event(
            session, run, "post_tool", tool_call_id=call.id, name=call.function.name, output=output
        )
        run.messages = [*run.messages, tool_result(call.id, output)]
        run.pending_calls = run.pending_calls[1:]
        # Remaining calls proposed alongside the question predate the answer.
        service.close_pending(session, run, "teacher_answer_changed")

    async def receive_join(self, session, run, call, answer):
        task = catalog.resolve(run.task_type)
        try:
            if not answer.get("ok"):
                raise DomainError(answer["error_code"], "Child join failed validation")
            await task.authorize(session, run)
            async with session.begin_nested():
                async with asyncio.timeout(task.registry.get(call.function.name).timeout_seconds):
                    result = task.registry.validate_result(call.function.name, answer["result"])
                    self.post_hook(call.function.name, result)
                    await task.apply_result(session, run, call.function.name, result)
            output = {"ok": True, "result": result}
        except (DomainError, TimeoutError) as exc:
            if inspect(run).expired_attributes:
                await session.refresh(run)
            output = {
                "ok": False,
                "error_code": exc.code if isinstance(exc, DomainError) else "tool_timeout",
            }
        service.event(
            session, run, "post_tool", tool_call_id=call.id, name=call.function.name, output=output
        )
        run.messages = [*run.messages, tool_result(call.id, output)]
        run.pending_calls = run.pending_calls[1:]
        run.status, run.resume_pending = "queued", True
        run.ready_at = await session.scalar(select(func.clock_timestamp()))

    async def invocation_remaining(self, lease):
        """Read only execution guards; never fetch transcripts or hold locks over HTTP."""
        root = aliased(AgentRun)
        async with self.db.sessions() as session:
            state = (
                await session.execute(
                    select(
                        AgentRun.status,
                        AgentRun.epoch,
                        AgentRun.lease_owner,
                        AgentRun.lease_until,
                        AgentRun.expires_at,
                        root.status.label("root_status"),
                        root.expires_at.label("root_expires_at"),
                        func.clock_timestamp().label("now"),
                    )
                    .join(root, root.id == func.coalesce(AgentRun.root_run_id, AgentRun.id))
                    .where(AgentRun.id == lease.run_id)
                )
            ).first()
        if (
            state is None
            or state.status != "running"
            or state.root_status not in scheduler.ACTIVE
            or state.epoch != lease.epoch
            or state.lease_owner != lease.owner
            or state.lease_until is None
            or state.lease_until <= state.now
        ):
            raise LostAgentLease()
        remaining = (min(state.expires_at, state.root_expires_at) - state.now).total_seconds()
        if remaining <= 0:
            raise ModelError("agent_deadline_exceeded", retryable=False)
        return remaining

    async def invoke(self, lease, body, policy, timeout_seconds):
        # Cancellation or process suspension can happen after the reservation was
        # committed. Validate before starting any potentially billable request.
        remaining = await self.invocation_remaining(lease)
        task = asyncio.create_task(self.build_model(policy).respond(body, policy))
        try:
            async with asyncio.timeout(min(timeout_seconds, remaining)):
                while not task.done():
                    done, _ = await asyncio.wait({task}, timeout=0.25)
                    if done:
                        break
                    await self.invocation_remaining(lease)
                return Reply.model_validate(await task)
        except TimeoutError:
            # Preserve a deadline/lease loss as such instead of retrying it as
            # a provider timeout. A genuine provider timeout still propagates.
            # Stop outbound work before this read, which can itself be delayed.
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await self.invocation_remaining(lease)
            raise
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def save_reply(self, lease, reply):
        try:
            async with self.db.sessions.begin() as session:
                run = await self.fenced(session, lease)
                dispatch = await session.scalar(
                    select(AgentEvent).where(
                        AgentEvent.run_id == run.id, AgentEvent.sequence == run.inflight_seq
                    )
                )
                service.event(
                    session,
                    run,
                    "model_succeeded",
                    call_sequence=run.inflight_seq,
                    metadata=reply.metadata.model_dump(mode="json"),
                )
                violation = await budgets.settle(
                    session, run, run.inflight_seq, reply.metadata.usage
                )
                run.inflight_seq = None
                if violation:
                    root = await session.get(AgentRun, run.root_run_id or run.id)
                    await scheduler.terminate_tree(session, root, violation)
                    return False
                if dispatch is not None and dispatch.body.get("purpose") == "context_summary":
                    task_definition = catalog.resolve(run.task_type)
                    await task_definition.authorize(session, run)
                    await accept_summary(
                        session, run, task_definition, dispatch.body["summary_plan"], reply
                    )
                    return True
                previous_ids = {
                    call["id"] for message in run.messages for call in message.get("tool_calls", [])
                }
                if previous_ids.intersection(call.id for call in reply.tool_calls):
                    service.finish(session, run, "failed", "duplicate_tool_call_id")
                    await scheduler.cancel_descendants(session, run, "parent_failed")
                    return False
                run.messages = [*run.messages, reply.message()]
                run.pending_calls = [call.model_dump(mode="json") for call in reply.tool_calls]
                if reply.tool_calls:
                    service.event(session, run, "tools_requested", calls=run.pending_calls)
                    return True
                task_definition = catalog.resolve(run.task_type)
                await task_definition.authorize(session, run)
                # Persisted artifact requirements cannot be loosened by extension hooks.
                artifact_exists = await task_definition.artifact_exists(session, run)
                accepted = artifact_exists and (
                    self.hooks is None or self.hooks.stop(task_definition.hook_context(run))
                )
                service.event(session, run, "stop_hook", accepted=accepted, reason="model_stop")
                if accepted:
                    service.finish(
                        session,
                        run,
                        getattr(task_definition, "completion_status", "waiting_teacher"),
                    )
                    return False
                if run.stop_repairs >= run.manifest.get("max_stop_repairs", 1):
                    service.finish(session, run, "failed", task_definition.missing_artifact_code)
                    await scheduler.cancel_descendants(session, run, "parent_failed")
                    return False
                run.stop_repairs += 1
                run.messages = [
                    *run.messages,
                    {
                        "role": "user",
                        "content": task_definition.repair_message,
                    },
                ]
                return True
        except DomainError as exc:
            # The provider response is already known even when authorization or
            # acceptance fails and rolls back its success receipt.
            failure = exc if isinstance(exc, ModelError) else ModelError(exc.code, retryable=False)
            failure.usage = reply.metadata.usage
            failure.returned_model = reply.metadata.returned_model
            failure.provider_request_id = reply.metadata.provider_request_id
            failure.provider_fingerprint = reply.metadata.provider_fingerprint
            failure.http_status = reply.metadata.http_status
            raise failure from None

    async def fail(self, lease, failure):
        async with self.db.sessions.begin() as session:
            # A deadline failure must still be recorded; do not renew an already lost lease.
            root, run = await scheduler.lock_run(session, lease.run_id)
            now = await session.scalar(select(func.clock_timestamp()))
            if (
                run is None
                or root.status not in scheduler.ACTIVE
                or run.status != "running"
                or run.epoch != lease.epoch
                or run.lease_owner != lease.owner
                or run.lease_until <= now
            ):
                return
            if run.inflight_seq is not None:
                violation = await budgets.settle(session, run, run.inflight_seq, failure.usage)
                if violation:
                    failure = ModelError(
                        violation,
                        retryable=False,
                        usage=failure.usage,
                        returned_model=failure.returned_model,
                        provider_request_id=failure.provider_request_id,
                        provider_fingerprint=failure.provider_fingerprint,
                        http_status=failure.http_status,
                    )
                kind = (
                    "model_unknown"
                    if failure.code
                    in {"provider_timeout", "provider_transport_error", "model_timeout"}
                    else "model_failed"
                )
                service.event(
                    session,
                    run,
                    kind,
                    call_sequence=run.inflight_seq,
                    error_code=failure.code,
                    usage=failure.usage,
                    returned_model=failure.returned_model,
                    provider_request_id=failure.provider_request_id,
                    provider_fingerprint=failure.provider_fingerprint,
                    http_status=failure.http_status,
                )
                run.inflight_seq = None
            if failure.code in {
                "agent_token_budget_exhausted",
                "agent_cost_budget_exhausted",
                "agent_usage_exceeds_reservation",
            }:
                await scheduler.terminate_tree(session, root, failure.code)
                return
            if (
                failure.retryable
                and run.attempts < 3
                and run.models_used < run.limits["max_model_calls"]
                and run.expires_at > now
            ):
                delay = max(
                    self.settings.retry_delay_seconds * 2 ** (run.attempts - 1),
                    failure.retry_after_seconds or 0,
                )
                run.status, run.last_error = "queued", failure.code
                run.ready_at = now + timedelta(seconds=delay)
                run.lease_owner, run.lease_until = None, None
                service.event(session, run, "retry_wait", error_code=failure.code)
            else:
                service.finish(session, run, "failed", failure.code)
                await scheduler.cancel_descendants(session, run, "parent_failed")

    async def execute(self, lease):
        try:
            while True:
                request, proceed = await self.prepare_step(lease)
                if not proceed:
                    return
                if request is not None:
                    reply = await self.invoke(lease, *request)
                    if not await self.save_reply(lease, reply):
                        return
        except LostAgentLease:
            return
        except ModelError as exc:
            await self.fail(lease, exc)
        except DomainError as exc:
            await self.fail(lease, ModelError(exc.code, retryable=False))
        except TimeoutError:
            await self.fail(lease, ModelError("model_timeout", retryable=True))
        # Process cancellation or database outage leaves the durable lease for recovery.

    async def run_once(self):
        lease = await self.claim()
        if lease is None:
            return False
        await self.execute(lease)
        return True

    async def run_forever(self):
        failures = 0
        while True:
            try:
                progressed = await self.run_once()
            except Exception as exc:
                # Keep provider data/SQL parameters out of logs. The lease remains recoverable.
                logging.getLogger(__name__).error("agent_worker_error: %s", type(exc).__name__)
                failures += 1
                await asyncio.sleep(failure_delay(self.settings.poll_seconds, failures))
                continue
            failures = 0
            if not progressed:
                await asyncio.sleep(self.settings.poll_seconds)

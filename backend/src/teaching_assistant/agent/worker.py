"""Durable model/tool loop with fenced, atomic tool effects and deny-only hooks."""

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select

from teaching_assistant import service as business
from teaching_assistant.agent import service
from teaching_assistant.agent.context import payload, tool_result
from teaching_assistant.agent.contracts import Reply, ToolCall
from teaching_assistant.agent.models import build_model
from teaching_assistant.agent.tools import DraftHooks, HookContext, execute_tool, validate_call
from teaching_assistant.db import AgentRun
from teaching_assistant.domain import DomainError, ModelError, digest


class LostAgentLease(Exception):
    pass


@dataclass(frozen=True)
class AgentLease:
    run_id: UUID
    epoch: int
    owner: str


class AgentWorker:
    def __init__(self, db, settings, *, run_ids=None, hooks=None):
        self.db, self.settings, self.run_ids = db, settings, run_ids
        self.owner = "agent-" + uuid4().hex
        self.hooks = hooks or DraftHooks()
        self.available = {p["id"]: p["manifest"] for p in service.profiles(settings) if p["ready"]}

    async def claim(self):
        while True:
            async with self.db.sessions.begin() as session:
                now = await session.scalar(select(func.clock_timestamp()))
                query = select(AgentRun).where(
                    or_(AgentRun.profile_id.in_(self.available), AgentRun.expires_at <= now),
                    AgentRun.status.in_(("queued", "running")),
                    or_(
                        AgentRun.expires_at <= now,
                        and_(AgentRun.status == "queued", AgentRun.ready_at <= now),
                        and_(AgentRun.status == "running", AgentRun.lease_until <= now),
                    ),
                )
                if self.run_ids is not None:
                    query = query.where(AgentRun.id.in_(self.run_ids))
                run = await session.scalar(
                    query.order_by(AgentRun.ready_at, AgentRun.id)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
                if run is None:
                    return None
                service.close_inflight(session, run, "lease_expired")
                if run.expires_at <= now or run.attempts >= 3:
                    service.finish(
                        session,
                        run,
                        "failed",
                        "agent_deadline_exceeded"
                        if run.expires_at <= now
                        else "agent_attempts_exhausted",
                    )
                    continue
                if run.manifest != self.available.get(run.profile_id):
                    service.finish(session, run, "failed", "agent_profile_mismatch")
                    continue
                run.epoch += 1
                run.attempts += 1
                run.status, run.last_error = "running", None
                run.lease_owner, run.lease_until = (
                    self.owner,
                    now + timedelta(seconds=self.settings.lease_seconds),
                )
                service.event(session, run, "claimed", attempt=run.attempts)
                return AgentLease(run.id, run.epoch, self.owner)

    async def fenced(self, session, lease):
        run = await session.scalar(
            select(AgentRun).where(AgentRun.id == lease.run_id).with_for_update()
        )
        now = await session.scalar(select(func.clock_timestamp()))
        if (
            run is None
            or run.status != "running"
            or run.epoch != lease.epoch
            or run.lease_owner != lease.owner
            or run.lease_until is None
            or run.lease_until <= now
        ):
            raise LostAgentLease()
        if run.expires_at <= now:
            raise ModelError("agent_deadline_exceeded", retryable=False)
        run.lease_until = now + timedelta(seconds=self.settings.lease_seconds)
        return run

    async def tool_step(self, session, run):
        call = ToolCall.model_validate(run.pending_calls[0])
        if run.tools_used >= run.limits["max_tool_calls"]:
            service.finish(session, run, "failed", "agent_tool_budget_exhausted")
            return
        run.tools_used += 1
        name = call.function.name
        try:
            arguments = validate_call(call)
            await business.owned_question(session, run.teacher_id, run.question_version_id)
            context = HookContext(run.question_read, run.draft_id is not None)
            blocked = self.hooks.pre_tool(name, context)
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
            async with session.begin_nested():
                async with asyncio.timeout(2):
                    result = await execute_tool(session, run, name, arguments)
                self.hooks.post_tool(name, result)
            if name == "read_question":
                run.question_read = True
            if name == "propose_rubric":
                run.draft_id = UUID(result["draft_id"])
                business.audit(
                    session,
                    run.teacher_id,
                    "agent_rubric_drafted",
                    run.draft_id,
                    run_id=str(run.id),
                )
            output = {"ok": True, "result": result}
        except DomainError as exc:
            output = {"ok": False, "error_code": exc.code, "detail": exc.detail}
        except TimeoutError:
            output = {
                "ok": False,
                "error_code": "tool_timeout",
                "detail": "Tool time limit exceeded",
            }
        service.event(session, run, "post_tool", tool_call_id=call.id, name=name, output=output)
        run.messages = [*run.messages, tool_result(call.id, output)]
        run.pending_calls = run.pending_calls[1:]
        # Draft save + tool result + waiting transition share one database transaction.
        if run.draft_id is not None:
            accepted = self.hooks.stop(HookContext(run.question_read, True))
            service.event(session, run, "stop_hook", accepted=accepted, reason="draft_saved")
            service.finish(
                session,
                run,
                "waiting_teacher" if accepted else "failed",
                None if accepted else "agent_stop_blocked",
            )

    async def prepare_step(self, lease):
        async with self.db.sessions.begin() as session:
            run = await self.fenced(session, lease)
            if run.pending_calls:
                await self.tool_step(session, run)
                return None, run.status == "running"
            if run.models_used >= run.limits["max_model_calls"]:
                service.finish(session, run, "failed", "agent_model_budget_exhausted")
                return None, False
            if run.tools_used >= run.limits["max_tool_calls"]:
                service.finish(session, run, "failed", "agent_tool_budget_exhausted")
                return None, False
            body = payload(run.messages, run.manifest, run.limits)
            now = await session.scalar(select(func.clock_timestamp()))
            run.models_used += 1
            sequence = service.event(
                session,
                run,
                "model_dispatched",
                prompt_hash=digest(body),
                requested_model=run.manifest["model"],
                ordinal=run.models_used,
            )
            run.inflight_seq = sequence
            timeout_seconds = min(
                run.manifest["call_timeout_seconds"], (run.expires_at - now).total_seconds()
            )
            return (body, run.manifest, timeout_seconds), True

    async def invoke(self, lease, body, policy, timeout_seconds):
        task = asyncio.create_task(build_model(self.settings, policy).respond(body, policy))
        try:
            async with asyncio.timeout(timeout_seconds):
                while not task.done():
                    done, _ = await asyncio.wait({task}, timeout=0.25)
                    if done:
                        break
                    async with self.db.sessions() as session:
                        state = (
                            await session.execute(
                                select(AgentRun.status, AgentRun.epoch).where(
                                    AgentRun.id == lease.run_id
                                )
                            )
                        ).first()
                    if state is None or state.status != "running" or state.epoch != lease.epoch:
                        raise LostAgentLease()
                return Reply.model_validate(await task)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def save_reply(self, lease, reply):
        async with self.db.sessions.begin() as session:
            run = await self.fenced(session, lease)
            service.event(
                session,
                run,
                "model_succeeded",
                call_sequence=run.inflight_seq,
                metadata=reply.metadata.model_dump(mode="json"),
            )
            run.inflight_seq = None
            previous_ids = {
                call["id"] for message in run.messages for call in message.get("tool_calls", [])
            }
            if previous_ids.intersection(call.id for call in reply.tool_calls):
                service.finish(session, run, "failed", "duplicate_tool_call_id")
                return False
            run.messages = [*run.messages, reply.message()]
            run.pending_calls = [call.model_dump(mode="json") for call in reply.tool_calls]
            if reply.tool_calls:
                service.event(session, run, "tools_requested", calls=run.pending_calls)
                return True
            accepted = self.hooks.stop(HookContext(run.question_read, run.draft_id is not None))
            # Mandatory artifact condition cannot be loosened by an extension hook.
            accepted = accepted and run.draft_id is not None
            service.event(session, run, "stop_hook", accepted=accepted, reason="model_stop")
            if accepted:
                service.finish(session, run, "waiting_teacher")
                return False
            if run.stop_repairs >= 1:
                service.finish(session, run, "failed", "agent_missing_draft")
                return False
            run.stop_repairs += 1
            run.messages = [
                *run.messages,
                {
                    "role": "user",
                    "content": "No draft was saved. Read the question and use propose_rubric; do not claim completion.",
                },
            ]
            return True

    async def fail(self, lease, failure):
        async with self.db.sessions.begin() as session:
            # A deadline failure must still be recorded; do not renew an already lost lease.
            run = await session.scalar(
                select(AgentRun).where(AgentRun.id == lease.run_id).with_for_update()
            )
            now = await session.scalar(select(func.clock_timestamp()))
            if (
                run is None
                or run.status != "running"
                or run.epoch != lease.epoch
                or run.lease_owner != lease.owner
                or run.lease_until <= now
            ):
                return
            if run.inflight_seq is not None:
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
        while True:
            try:
                if await self.run_once():
                    continue
            except Exception as exc:
                # Keep provider data/SQL parameters out of logs. The lease remains recoverable.
                logging.getLogger(__name__).error("agent_worker_error: %s", type(exc).__name__)
            await asyncio.sleep(self.settings.poll_seconds)

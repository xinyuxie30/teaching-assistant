"""Scoped background children and explicit durable joins, under the caller's root lock."""

from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, ValidationError, model_validator
from sqlalchemy import select

from teaching_assistant.db import AgentArtifact, AgentRun
from teaching_assistant.domain import Contract, DomainError, digest
from teaching_assistant.harness.contracts import ToolCall
from teaching_assistant.harness.delegation import ChildTaskRequest
from teaching_assistant.model_http import strict_json

BACKGROUND_VERSION = "durable-background-children-v1"


@dataclass(frozen=True)
class BackgroundChildRequest:
    task_type: str
    task_input: dict
    instructions: str


@dataclass(frozen=True)
class ChildJoinRequest:
    child_run_id: UUID


class ChildReferenceInput(Contract):
    child_run_id: UUID


class BackgroundSpawnResult(Contract):
    child_run_id: UUID
    task_type: str = Field(min_length=1, max_length=80)
    status: Literal["scheduled"] = "scheduled"


class ChildArtifactOutput(Contract):
    artifact_id: UUID
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    body: dict[str, Any]
    confirmed: Literal[False] = False


class ChildOutput(Contract):
    child_run_id: UUID
    task_type: str = Field(min_length=1, max_length=80)
    status: Literal["not_ready", "success", "failed"]
    run_status: Literal[
        "queued",
        "running",
        "waiting_children",
        "waiting_input",
        "waiting_timer",
        "waiting_teacher",
        "completed",
        "failed",
        "cancelled",
    ]
    artifact: ChildArtifactOutput | None = None
    error_code: str | None = Field(default=None, min_length=1, max_length=120)

    @model_validator(mode="after")
    def result_shape(self):
        if self.status == "success":
            if self.run_status != "completed" or self.artifact is None or self.error_code:
                raise ValueError("Successful output requires a completed, verified artifact")
        elif self.artifact is not None or (self.status == "failed") != bool(self.error_code):
            raise ValueError("Only successful child outputs contain an artifact")
        elif (self.status == "not_ready") != (
            self.run_status
            in {"queued", "running", "waiting_children", "waiting_input", "waiting_timer"}
        ):
            raise ValueError("Child readiness must agree with its persisted status")
        elif self.run_status == "completed":
            raise ValueError("A completed child requires verified successful output")
        return self


def require_supported(run):
    if run.parent_run_id is not None:
        raise DomainError("agent_root_required", "Background delegation belongs to the root task")
    if run.manifest.get("background_version") != BACKGROUND_VERSION:
        raise DomainError(
            "agent_background_unsupported", "This pinned runtime has no background tools"
        )


async def owned_child(session, run, child_id):
    from teaching_assistant.harness import catalog

    require_supported(run)
    await catalog.resolve(run.task_type).authorize(session, run)
    child = await session.scalar(
        select(AgentRun).where(
            AgentRun.id == child_id,
            AgentRun.parent_run_id == run.id,
            AgentRun.root_run_id == run.id,
            AgentRun.teacher_id == run.teacher_id,
        )
    )
    if child is None:
        raise DomainError("agent_child_not_found", "Child task does not belong to this parent", 404)
    if (
        child.task_input != run.task_input
        or child.task_type not in run.manifest.get("allowed_children", ())
        or child.task_type not in catalog.ALLOWED_CHILDREN.get(run.task_type, ())
        or child.depth != run.depth + 1
    ):
        raise DomainError("invalid_child_scope", "Child task does not preserve its parent's scope")
    await catalog.resolve(child.task_type).authorize(session, child)
    return child


async def spawn_child(session, run, call, request, settings):
    from teaching_assistant.harness import scheduler

    require_supported(run)
    if not isinstance(request, BackgroundChildRequest):
        raise DomainError(
            "invalid_child_request", "Only registered code may spawn a background child"
        )
    child = await scheduler.spawn_child(
        session,
        run,
        call,
        ChildTaskRequest(request.task_type, request.task_input, request.instructions),
        settings,
        defer_wait=True,
    )
    return BackgroundSpawnResult(child_run_id=child.id, task_type=child.task_type).model_dump(
        mode="json"
    )


async def get_child_output(session, run, arguments: ChildReferenceInput):
    from teaching_assistant.harness import catalog, scheduler

    child = await owned_child(session, run, arguments.child_run_id)
    fields = {"child_run_id": child.id, "task_type": child.task_type, "run_status": child.status}
    if child.status in (*scheduler.ACTIVE, "waiting_input", "waiting_timer"):
        return ChildOutput(**fields, status="not_ready").model_dump(mode="json")
    if child.status != "completed":
        return ChildOutput(
            **fields, status="failed", error_code=child.last_error or "agent_child_failed"
        ).model_dump(mode="json")
    artifact = await session.scalar(select(AgentArtifact).where(AgentArtifact.run_id == child.id))
    if (
        artifact is None
        or artifact.kind != child.task_type
        or artifact.confirmed_at is not None
        or artifact.content_hash != digest(artifact.body)
        or not await catalog.resolve(child.task_type).artifact_exists(session, child)
    ):
        raise DomainError("invalid_child_result", "The child has no valid unconfirmed artifact")
    return ChildOutput(
        **fields,
        status="success",
        artifact=ChildArtifactOutput(
            artifact_id=artifact.id, content_hash=artifact.content_hash, body=artifact.body
        ),
    ).model_dump(mode="json")


async def join_child(session, run, arguments: ChildReferenceInput):
    require_supported(run)
    return ChildJoinRequest(arguments.child_run_id)


def _join_arguments(call):
    try:
        if call.function.name != "join_child":
            raise ValueError
        return ChildReferenceInput.model_validate(strict_json(call.function.arguments))
    except (ValidationError, ValueError, TypeError, RecursionError):
        raise DomainError("invalid_child_join", "The pending child join is invalid") from None


async def begin_join(session, run, call, request):
    from teaching_assistant.harness import store

    require_supported(run)
    if (
        not isinstance(request, ChildJoinRequest)
        or run.status != "running"
        or not run.pending_calls
        or run.pending_calls[0] != call.model_dump(mode="json")
        or _join_arguments(call).child_run_id != request.child_run_id
    ):
        raise DomainError("invalid_child_join", "A join requires the current pending tool call")
    child = await owned_child(session, run, request.child_run_id)
    result = await get_child_output(session, run, ChildReferenceInput(child_run_id=child.id))
    if result["status"] != "not_ready":
        return result
    state = dict(run.task_state or {})
    joins = dict(state.get("background_joins", {}))
    binding = {"child_run_id": str(child.id), "spawn_tool_call_id": child.parent_tool_call_id}
    if call.id in joins and joins[call.id] != binding:
        raise DomainError("invalid_child_join", "Saved join does not match its child")
    joins[call.id] = binding
    state["background_joins"] = joins
    run.task_state = state
    run.status, run.resume_pending = "waiting_children", False
    run.lease_owner = run.lease_until = None
    store.event(
        session,
        run,
        "waiting_children",
        child_run_id=str(child.id),
        tool_call_id=call.id,
        mode="explicit_join",
    )
    return None


def is_join_wait(run):
    if not run.pending_calls:
        return False
    call = run.pending_calls[0]
    return call.get("id") in (run.task_state or {}).get("background_joins", {})


async def pending_join(session, run):
    """Read a ready result without removing the pending call or charging its tool again."""
    if not is_join_wait(run):
        return None
    require_supported(run)
    call = ToolCall.model_validate(run.pending_calls[0])
    arguments = _join_arguments(call)
    binding = run.task_state["background_joins"][call.id]
    child = await owned_child(session, run, arguments.child_run_id)
    if binding != {"child_run_id": str(child.id), "spawn_tool_call_id": child.parent_tool_call_id}:
        raise DomainError("invalid_child_join", "Saved join no longer matches its child")
    result = await get_child_output(session, run, arguments)
    if result["status"] == "not_ready":
        return None
    return call, {"ok": True, "result": result}

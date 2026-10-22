"""Code-owned requests for a durable, paired answer from the current teacher."""

from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from pydantic import Field, StringConstraints, ValidationError, field_validator
from sqlalchemy import select

from teaching_assistant.db import AgentWait, AgentWake
from teaching_assistant.domain import Contract, DomainError, digest
from teaching_assistant.harness.contracts import ToolCall
from teaching_assistant.model_http import strict_json

INTERACTIVE_VERSION = "teacher-questions-v1"
QuestionText = Annotated[str, StringConstraints(min_length=1, max_length=1000)]
AnswerText = Annotated[str, StringConstraints(min_length=1, max_length=4000)]
OptionText = Annotated[str, StringConstraints(min_length=1, max_length=200)]


class AskTeacherInput(Contract):
    prompt: QuestionText
    options: list[OptionText] | None = Field(default=None, min_length=2, max_length=4)

    @field_validator("prompt")
    @classmethod
    def nonblank_prompt(cls, value):
        if not value.strip():
            raise ValueError("Question must contain non-whitespace text")
        return value

    @field_validator("options")
    @classmethod
    def distinct_options(cls, value):
        if value is not None:
            labels = [item.strip() for item in value]
            if any(not label for label in labels) or len(set(labels)) != len(labels):
                raise ValueError("Question options must be distinct and nonblank")
        return value


class TeacherAnswerResult(Contract):
    question: QuestionText
    response: AnswerText
    wait_token: UUID

    @field_validator("question", "response")
    @classmethod
    def nonblank_text(cls, value):
        if not value.strip():
            raise ValueError("Question and answer must contain non-whitespace text")
        return value


class QuestionSnapshot(AskTeacherInput):
    tool_call_id: str = Field(min_length=1, max_length=120)


@dataclass(frozen=True)
class TeacherInputRequest:
    """Control flow returned by registered Python code, never a model JSON instruction."""

    prompt: str
    options: tuple[str, ...] | None = None


def require_supported(run):
    if run.parent_run_id is not None:
        raise DomainError("agent_root_required", "Only the root task may ask its teacher", 409)
    if run.manifest.get("interactive_version") != INTERACTIVE_VERSION:
        raise DomainError(
            "agent_question_unsupported", "This pinned runtime cannot request teacher input", 409
        )


async def ask_teacher(session, run, arguments: AskTeacherInput):
    require_supported(run)
    return TeacherInputRequest(
        arguments.prompt, tuple(arguments.options) if arguments.options is not None else None
    )


def _call_question(call):
    try:
        if call.function.name != "ask_teacher":
            raise ValueError
        return AskTeacherInput.model_validate(strict_json(call.function.arguments))
    except (ValidationError, ValueError, TypeError, RecursionError):
        raise DomainError("invalid_teacher_question", "The pending question is invalid") from None


def validate_snapshot(saved, call):
    """Verify the immutable wait and its binding to the original tool arguments."""
    from teaching_assistant.harness import waiting

    try:
        question = QuestionSnapshot.model_validate(saved.snapshot.get("question"))
    except ValidationError:
        raise DomainError("agent_question_corrupt", "Saved teacher question is invalid") from None
    if (
        saved.kind != "waiting_input"
        or saved.wake_at is not None
        or saved.snapshot.get("version") != waiting.WAIT_VERSION
        or digest(saved.snapshot) != saved.content_hash
        or question.tool_call_id != call.id
        or question.model_dump(exclude={"tool_call_id"}) != _call_question(call).model_dump()
    ):
        raise DomainError("agent_question_corrupt", "Saved teacher question failed verification")
    return question


async def begin_question(session, run, call, request):
    """Pause under the caller's root lock, retaining the original pending tool call."""
    # Runtime services depend on the task catalog, which imports these contracts.
    # Load them only when executing, so adapter/worker entry points are independent.
    from teaching_assistant.harness import waiting

    require_supported(run)
    if not isinstance(request, TeacherInputRequest):
        raise DomainError("invalid_teacher_question", "Only registered code may ask the teacher")
    if (
        run.status != "running"
        or not run.pending_calls
        or run.pending_calls[0] != call.model_dump(mode="json")
    ):
        raise DomainError("invalid_teacher_question", "A question requires the current tool call")
    arguments = _call_question(call)
    if request.prompt != arguments.prompt or request.options != (
        tuple(arguments.options) if arguments.options is not None else None
    ):
        raise DomainError("invalid_teacher_question", "Question differs from its tool arguments")
    original_calls = [
        item
        for message in run.messages
        if message.get("role") == "assistant"
        for item in message.get("tool_calls", [])
        if item.get("id") == call.id
    ]
    if original_calls != [call.model_dump(mode="json")] or any(
        message.get("role") == "tool" and message.get("tool_call_id") == call.id
        for message in run.messages
    ):
        raise DomainError("invalid_teacher_question", "Question tool history is inconsistent")
    existing = await session.scalar(
        select(AgentWait.id).where(
            AgentWait.run_id == run.id,
            AgentWait.snapshot["question"]["tool_call_id"].astext == call.id,
        )
    )
    if existing is not None:
        raise DomainError("agent_question_already_requested", "This tool call already has a wait")
    question = QuestionSnapshot(tool_call_id=call.id, **arguments.model_dump()).model_dump(
        mode="json"
    )
    return await waiting.pause_tree(session, run, reason=request.prompt, question=question)


async def pending_answer(session, run):
    """Read a committed answer; the worker atomically validates and pairs its receipt."""
    from teaching_assistant.harness import scheduler

    if not run.pending_calls or run.status not in scheduler.ACTIVE:
        return None
    call = ToolCall.model_validate(run.pending_calls[0])
    if call.function.name != "ask_teacher":
        return None
    rows = (
        await session.execute(
            select(AgentWait, AgentWake)
            .outerjoin(AgentWake, AgentWake.wait_id == AgentWait.id)
            .where(
                AgentWait.run_id == run.id,
                AgentWait.snapshot["question"]["tool_call_id"].astext == call.id,
            )
        )
    ).all()
    if not rows:
        return None
    require_supported(run)
    if len(rows) != 1:
        raise DomainError("agent_question_corrupt", "Question has more than one saved wait")
    saved, receipt = rows[0]
    question = validate_snapshot(saved, call)
    if receipt is None:
        return None
    if (
        receipt.source != "teacher"
        or not receipt.response
        or not receipt.response.strip()
        or receipt.request_hash
        != digest({"wait_token": str(saved.id), "source": "teacher", "response": receipt.response})
    ):
        raise DomainError("agent_question_corrupt", "Saved teacher answer failed verification")
    result = TeacherAnswerResult(
        question=question.prompt, response=receipt.response, wait_token=saved.id
    ).model_dump(mode="json")
    return call, {"ok": True, "result": result}

"""Small typed registry, hard policy first, code-only deny hooks, transactional handlers."""

from dataclasses import dataclass

from pydantic import ValidationError

from teaching_assistant import service
from teaching_assistant.domain import Contract, DomainError
from teaching_assistant.model_http import strict_json
from teaching_assistant.schemas import DraftInput

TOOL_VERSION = "rubric-tools-v1"
HOOK_VERSION = "draft-review-hooks-v1"


class NoArguments(Contract):
    pass


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_type: type[Contract]
    effect: str

    def schema(self):
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_type.model_json_schema(),
            },
        }


TOOLS = {
    "read_question": ToolSpec(
        "read_question",
        "Read the exact question, reference and maximum points bound to this task.",
        NoArguments,
        "read",
    ),
    "propose_rubric": ToolSpec(
        "propose_rubric",
        "Save one unconfirmed rubric draft for this task's question. The teacher must confirm it. "
        "Levels run from lowest to highest: ordinal 0 is the lowest level, ordinals are "
        "consecutive, points must not decrease with ordinal, and the top level must equal the "
        "criterion maximum. Criterion maxima must sum to the question maximum.",
        DraftInput,
        "draft_write",
    ),
}


def schemas():
    return [tool.schema() for tool in TOOLS.values()]


def validate_call(call):
    # Neither a model nor a hook may register tools or provide ownership/scope parameters.
    if call.function.name not in TOOLS:
        raise DomainError("tool_not_allowed", "This task cannot use that tool")
    try:
        payload = strict_json(call.function.arguments)
    except (ValueError, TypeError, RecursionError):
        raise DomainError(
            "invalid_tool_arguments",
            "Arguments must be JSON without duplicate keys or non-finite numbers",
        ) from None
    try:
        return TOOLS[call.function.name].input_type.model_validate(payload)
    except ValidationError as exc:
        # Return the contract reason, never the submitted values or a stack trace.
        first = exc.errors()[0]
        path = ".".join(str(part) for part in first["loc"]) or "arguments"
        detail = f"Arguments violate the tool schema at {path}: {first['msg']}"
        raise DomainError("invalid_tool_arguments", detail[:300]) from None


@dataclass(frozen=True)
class HookContext:
    question_read: bool
    draft_exists: bool


class DraftHooks:
    """Versioned in-process hooks can block; they cannot override hard authorization."""

    def pre_tool(self, name, context: HookContext) -> str | None:
        if name == "propose_rubric" and not context.question_read:
            return "read_question_required"
        if context.draft_exists:
            return "teacher_review_required"
        return None

    def post_tool(self, name, result) -> None:
        if name == "propose_rubric" and result.get("confirmed") is not False:
            raise DomainError("invalid_tool_result", "Draft tools cannot confirm a rubric")

    def stop(self, context: HookContext) -> bool:
        return context.draft_exists


async def execute_tool(session, run, name, arguments):
    # Identity and question are server-bound, never model-controlled arguments.
    question = await service.owned_question(session, run.teacher_id, run.question_version_id)
    if name == "read_question":
        return {
            "question": question.prompt,
            "reference": question.reference,
            "maximum_points": str(question.maximum_points),
            "content_hash": question.content_hash,
            "version": question.version,
        }
    if name == "propose_rubric":
        saved = await service.create_draft(session, run.teacher_id, question.id, arguments)
        return {"draft_id": saved["id"], "revision": saved["revision"], "confirmed": False}
    raise DomainError("tool_not_allowed", "Unknown tool")

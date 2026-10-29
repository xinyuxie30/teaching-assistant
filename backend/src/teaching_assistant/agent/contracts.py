from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from teaching_assistant.domain import Contract
from teaching_assistant.harness.contracts import FunctionCall, Reply, ToolCall
from teaching_assistant.schemas import PageQuery

__all__ = ["AgentQuery", "RunInput", "CancelInput", "FunctionCall", "ToolCall", "Reply"]


class AgentQuery(PageQuery):
    limit: int = Field(default=20, ge=1, le=100)
    question_version_id: UUID | None = None
    task_type: (
        Literal[
            "rubric",
            "lesson-plan",
            "lesson-plan-team",
            "lesson-practice",
            "lesson-plan-parallel",
            "lesson-practice-designer",
            "lesson-review",
            "lesson-plan-assisted",
            "lesson-plan-research",
            "lesson-plan-background",
            "class-practice",
            "homework-grading",
        ]
        | None
    ) = None
    status: (
        Literal[
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
        | None
    ) = None


class RunInput(Contract):
    source_draft_id: UUID | None = None
    agent_profile: Literal["fake-rubric-agent-v1", "deepseek-rubric-agent-v1"] = (
        "fake-rubric-agent-v1"
    )
    instructions: str = Field(
        default="请为本题起草可区分的分项评分细则。", min_length=1, max_length=4000
    )
    max_model_calls: int = Field(default=6, ge=1, le=12)
    max_tool_calls: int = Field(default=10, ge=1, le=24)
    max_context_characters: int = Field(default=40000, ge=2000, le=200000)
    max_seconds: int = Field(default=300, ge=10, le=3600)
    max_total_tokens: int | None = Field(default=None, ge=1, le=10000000, strict=True)
    max_cost_usd: Decimal | None = Field(
        default=None, gt=0, le=100, max_digits=12, decimal_places=8
    )

    @field_validator("instructions")
    @classmethod
    def nonblank_instructions(cls, value):
        if not value.strip():
            raise ValueError("Instructions must contain non-whitespace text")
        return value

    @model_validator(mode="after")
    def revision_requires_feedback(self):
        if self.source_draft_id is not None and "instructions" not in self.model_fields_set:
            raise ValueError("Draft revisions require explicit teacher instructions")
        return self


class CancelInput(Contract):
    reason: str = Field(default="teacher_cancelled", min_length=1, max_length=500)


class ArtifactConfirmInput(Contract):
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class WaitInput(Contract):
    reason: str = Field(min_length=1, max_length=1000)
    wake_at: AwareDatetime | None = None

    @field_validator("reason")
    @classmethod
    def nonblank_reason(cls, value):
        if not value.strip():
            raise ValueError("Wait reason must contain non-whitespace text")
        return value


class WakeInput(Contract):
    wait_token: UUID
    response: str | None = Field(default=None, min_length=1, max_length=4000)

    @field_validator("response")
    @classmethod
    def nonblank_response(cls, value):
        if value is not None and not value.strip():
            raise ValueError("Wake response must contain non-whitespace text")
        return value

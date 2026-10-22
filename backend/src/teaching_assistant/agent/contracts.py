from typing import Literal

from pydantic import Field, model_validator

from teaching_assistant.domain import Contract, ModelMetadata


class RunInput(Contract):
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


class CancelInput(Contract):
    reason: str = Field(default="teacher_cancelled", min_length=1, max_length=500)


class FunctionCall(Contract):
    name: str = Field(min_length=1, max_length=128)
    arguments: str = Field(max_length=100000)


class ToolCall(Contract):
    id: str = Field(min_length=1, max_length=120)
    type: Literal["function"] = "function"
    function: FunctionCall


class Reply(Contract):
    content: str | None = Field(default=None, max_length=50000)
    tool_calls: list[ToolCall] = Field(default_factory=list, max_length=4)
    metadata: ModelMetadata

    @model_validator(mode="after")
    def distinct_calls(self):
        if len({call.id for call in self.tool_calls}) != len(self.tool_calls):
            raise ValueError("Duplicate tool call IDs")
        return self

    def message(self):
        result = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            result["tool_calls"] = [call.model_dump(mode="json") for call in self.tool_calls]
        return result

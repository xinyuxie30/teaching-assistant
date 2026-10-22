"""Provider-neutral model replies and tool calls used by the Harness."""

from typing import Literal

from pydantic import Field, model_validator

from teaching_assistant.domain import Contract, ModelMetadata


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

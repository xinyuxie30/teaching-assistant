"""Immutable, typed tool contracts shared by every Harness task."""

import math
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from pydantic import ValidationError

from teaching_assistant.domain import Contract, DomainError
from teaching_assistant.model_http import strict_json


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_type: type[Contract]
    effect: str
    handler: Callable[..., Awaitable[Any]]
    output_type: type[Contract]
    version: str = "1"
    timeout_seconds: float = 2

    def schema(self):
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_type.model_json_schema(),
            },
        }


class ToolRegistry:
    """Only application code can register tools; model input cannot expand this set."""

    def __init__(self, specs: Iterable[ToolSpec]):
        indexed = {}
        for spec in specs:
            if not spec.name or spec.name in indexed:
                raise ValueError("Tool names must be non-empty and unique")
            if not math.isfinite(spec.timeout_seconds) or spec.timeout_seconds <= 0:
                raise ValueError("Tool timeout must be finite and positive")
            if not callable(spec.handler):
                raise ValueError("A tool requires a registered handler")
            indexed[spec.name] = spec
        self._specs = MappingProxyType(indexed)

    def get(self, name: str) -> ToolSpec:
        try:
            return self._specs[name]
        except KeyError:
            raise DomainError("tool_not_allowed", "This task cannot use that tool") from None

    def schemas(self):
        return [spec.schema() for spec in self._specs.values()]

    def manifest(self):
        return [
            {
                "name": spec.name,
                "version": spec.version,
                "description": spec.description,
                "input_schema": spec.input_type.model_json_schema(),
                "output_schema": spec.output_type.model_json_schema(),
                "effect": spec.effect,
                "timeout_seconds": spec.timeout_seconds,
            }
            for spec in self._specs.values()
        ]

    def validate_call(self, call):
        spec = self.get(call.function.name)
        try:
            payload = strict_json(call.function.arguments)
        except (ValueError, TypeError, RecursionError):
            raise DomainError(
                "invalid_tool_arguments",
                "Arguments must be JSON without duplicate keys or non-finite numbers",
            ) from None
        try:
            return spec.input_type.model_validate(payload)
        except ValidationError as exc:
            first = exc.errors()[0]
            path = ".".join(str(part) for part in first["loc"]) or "arguments"
            detail = f"Arguments violate the tool schema at {path}: {first['msg']}"
            raise DomainError("invalid_tool_arguments", detail[:300]) from None

    def validate_result(self, name: str, result) -> dict:
        try:
            return self.get(name).output_type.model_validate(result).model_dump(mode="json")
        except ValidationError:
            # A tool implementation may contain sensitive values: never echo its failed output.
            raise DomainError(
                "invalid_tool_result", "Tool result violates its output schema"
            ) from None

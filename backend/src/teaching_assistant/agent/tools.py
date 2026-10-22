"""Compatibility facade for rubric tools now registered through the Harness."""

from types import MappingProxyType

from teaching_assistant.harness.rubric import (
    HOOK_VERSION,
    REGISTRY,
    TOOL_VERSION,
    DraftHooks,
    HookContext,
    NoArguments,
)
from teaching_assistant.harness.tools import ToolSpec

__all__ = [
    "HOOK_VERSION",
    "TOOL_VERSION",
    "TOOLS",
    "DraftHooks",
    "HookContext",
    "NoArguments",
    "ToolSpec",
    "execute_tool",
    "schemas",
    "validate_call",
]

TOOLS = MappingProxyType({name: REGISTRY.get(name) for name in ("read_question", "propose_rubric")})


def schemas():
    return REGISTRY.schemas()


def validate_call(call):
    return REGISTRY.validate_call(call)


async def execute_tool(session, run, name, arguments):
    result = await REGISTRY.get(name).handler(session, run, arguments)
    return REGISTRY.validate_result(name, result)

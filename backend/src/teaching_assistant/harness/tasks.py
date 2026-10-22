"""Task adapters own domain behavior; the durable worker owns execution semantics."""

from typing import Any, Protocol

from teaching_assistant.harness.tools import ToolRegistry


class TaskDefinition(Protocol):
    key: str
    version: str
    system: str
    registry: ToolRegistry
    waiting_for: str | None
    repair_message: str
    missing_artifact_code: str

    async def authorize(self, session, run) -> None: ...

    def pre_tool(self, run, name: str) -> str | None: ...

    async def apply_result(self, session, run, name: str, result: dict) -> None: ...

    async def artifact_exists(self, session, run) -> bool: ...

    def hook_context(self, run) -> Any: ...

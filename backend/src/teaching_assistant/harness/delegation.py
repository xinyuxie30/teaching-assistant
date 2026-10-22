"""A code-owned control result, never parsed from model output or tool JSON."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ChildTaskRequest:
    task_type: str
    task_input: dict
    instructions: str

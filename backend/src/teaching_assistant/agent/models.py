"""Compatibility facade for provider adapters now shared by Harness tasks."""

from teaching_assistant.harness.models import DeepSeekAgentModel, build_model
from teaching_assistant.harness.rubric_model import FakeAgentModel

__all__ = ["DeepSeekAgentModel", "FakeAgentModel", "build_model"]

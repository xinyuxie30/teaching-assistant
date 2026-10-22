"""Compatibility entry point for existing rubric callers and worker integrations."""

from teaching_assistant.agent.models import build_model
from teaching_assistant.harness.worker import AgentLease, HarnessWorker, LostAgentLease

__all__ = ["AgentLease", "AgentWorker", "LostAgentLease"]


class AgentWorker(HarnessWorker):
    def build_model(self, policy):
        # Keep the established integration seam for provider overrides and fake-model tests.
        return build_model(self.settings, policy)

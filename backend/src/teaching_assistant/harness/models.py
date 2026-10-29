"""Single-call model adapters. Tool execution and retries remain in our harness."""

import httpx
from pydantic import ValidationError

from teaching_assistant.deepseek import _Usage
from teaching_assistant.domain import ModelError, ModelMetadata, digest
from teaching_assistant.harness.assisted_lesson import FakeAssistedLessonModel
from teaching_assistant.harness.background_lesson import FakeBackgroundLessonModel
from teaching_assistant.harness.class_practice import FakeClassPracticeModel
from teaching_assistant.harness.contracts import Reply
from teaching_assistant.harness.grading import FakeGradingModel
from teaching_assistant.harness.lesson import FakeLessonModel
from teaching_assistant.harness.parallel_lesson import (
    FakeLessonReviewModel,
    FakeParallelLessonModel,
    FakePracticeDesignerModel,
)
from teaching_assistant.harness.practice import FakePracticeModel
from teaching_assistant.harness.research_lesson import FakeResearchLessonModel
from teaching_assistant.harness.rubric_model import FakeAgentModel
from teaching_assistant.harness.semantic_compaction import fake_summary_reply, is_summary_request
from teaching_assistant.harness.team_lesson import FakeTeamLessonModel
from teaching_assistant.model_http import post_json


class DeepSeekAgentModel:
    def __init__(self, settings, *, client=None):
        self.settings, self.client = settings, client

    @staticmethod
    def _tool_calls(value):
        """Keep the contract fields; providers may add fields such as ``index``."""
        if not isinstance(value, list):
            raise ValueError("Tool calls must be an array")
        normalized = []
        for call in value:
            if not isinstance(call, dict):
                raise ValueError("Tool call must be an object")
            normalized.append({key: call[key] for key in ("id", "type", "function") if key in call})
        return normalized

    async def respond(self, body, policy):
        if not self.settings.deepseek_api_key:
            raise ModelError("model_not_configured", retryable=False)
        kwargs = {
            "endpoint": policy["endpoint"],
            "payload": body,
            "api_key": self.settings.deepseek_api_key.get_secret_value(),
            "timeout_seconds": policy["call_timeout_seconds"],
        }
        if self.client is not None:
            raw, request_id, status = await post_json(self.client, **kwargs)
        else:
            async with httpx.AsyncClient(
                proxy=self.settings.deepseek_proxy, trust_env=False
            ) as client:
                raw, request_id, status = await post_json(client, **kwargs)
        returned, fingerprint, usage = raw.get("model"), raw.get("system_fingerprint"), None
        returned = returned[:120] if isinstance(returned, str) else None
        fingerprint = fingerprint[:255] if isinstance(fingerprint, str) else None
        request_id = request_id or (raw["id"][:255] if isinstance(raw.get("id"), str) else None)
        try:
            if raw.get("usage") is not None:
                usage = _Usage.model_validate(raw["usage"]).normalized()
            if raw.get("model") != policy["expected_returned_model"]:
                raise ModelError("provider_model_version_mismatch", retryable=False)
            choices = raw.get("choices")
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError("One choice required")
            choice = choices[0]
            if choice["finish_reason"] not in {"stop", "tool_calls"}:
                raise ModelError("provider_incomplete_response", retryable=False)
            message = choice["message"]
            if (
                message.get("role") != "assistant"
                or message.get("refusal")
                or message.get("function_call")
            ):
                raise ValueError("Invalid assistant message")
            calls = message.get("tool_calls")
            if calls is None:
                calls = []
            calls = self._tool_calls(calls)
            if bool(calls) != (choice["finish_reason"] == "tool_calls"):
                raise ValueError("Finish reason does not match tool calls")
            return Reply(
                content=message.get("content"),
                tool_calls=calls,
                metadata=ModelMetadata(
                    requested_model=policy["model"],
                    returned_model=returned,
                    adapter_version="deepseek-tool-agent-v1",
                    usage=usage,
                    provider_request_id=request_id,
                    provider_fingerprint=fingerprint,
                    prompt_hash=digest(body),
                    http_status=status,
                ),
            )
        except (
            ModelError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            ValidationError,
        ) as exc:
            failure = (
                exc
                if isinstance(exc, ModelError)
                else ModelError("provider_invalid_response", retryable=True)
            )
            failure.usage, failure.returned_model = usage, returned
            failure.provider_fingerprint, failure.provider_request_id = fingerprint, request_id
            failure.http_status = status
            raise failure from None


class FakeHarnessModel:
    def __init__(self, task_model):
        self.task_model = task_model

    async def respond(self, body, policy):
        if is_summary_request(body):
            return fake_summary_reply(body, policy)
        return await self.task_model.respond(body, policy)


def build_model(settings, policy):
    if policy["provider"] == "deepseek":
        return DeepSeekAgentModel(settings)
    if policy["provider"] != "fake":
        raise ModelError("unknown_agent_provider", retryable=False)
    task_type = policy.get("task_type", "rubric")
    task_models = {
        "homework-grading": FakeGradingModel,
        "class-practice": FakeClassPracticeModel,
        "rubric": FakeAgentModel,
        "lesson-plan": FakeLessonModel,
        "lesson-plan-team": FakeTeamLessonModel,
        "lesson-practice": FakePracticeModel,
        "lesson-plan-parallel": FakeParallelLessonModel,
        "lesson-practice-designer": FakePracticeDesignerModel,
        "lesson-review": FakeLessonReviewModel,
        "lesson-plan-assisted": FakeAssistedLessonModel,
        "lesson-plan-research": FakeResearchLessonModel,
        "lesson-plan-background": FakeBackgroundLessonModel,
    }
    if task_type not in task_models:
        raise ModelError("unknown_task_type", retryable=False)
    return FakeHarnessModel(task_models[task_type]())

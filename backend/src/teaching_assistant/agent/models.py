"""Single-call model adapters. Tool execution and retries remain in our harness."""

import json

import httpx
from pydantic import ValidationError

from teaching_assistant.agent.contracts import Reply
from teaching_assistant.deepseek import _Usage
from teaching_assistant.domain import ModelError, ModelMetadata, digest
from teaching_assistant.model_http import post_json


class FakeAgentModel:
    async def respond(self, body, policy):
        question = next(
            (
                output["result"]
                for m in body["messages"]
                if m["role"] == "tool"
                for output in [json.loads(m["content"])]
                if output.get("ok") and "maximum_points" in output.get("result", {})
            ),
            None,
        )
        if question is None:
            name, arguments = "read_question", {}
        else:
            name = "propose_rubric"
            arguments = {
                "rubric": {
                    "global_instructions": "模拟草稿，仅用于工程验证，老师必须检查并确认。",
                    "criteria": [
                        {
                            "id": "demo",
                            "description": "模拟评分点（不代表教学标准）",
                            "maximum_points": question["maximum_points"],
                            "levels": [
                                {
                                    "id": "missing",
                                    "ordinal": 0,
                                    "description": "模拟未体现",
                                    "points": "0.00",
                                },
                                {
                                    "id": "complete",
                                    "ordinal": 1,
                                    "description": "模拟完整体现",
                                    "points": question["maximum_points"],
                                },
                            ],
                        }
                    ],
                }
            }
        return Reply.model_validate(
            {
                "tool_calls": [
                    {
                        "id": f"fake-{len(body['messages'])}",
                        "function": {
                            "name": name,
                            "arguments": json.dumps(arguments, ensure_ascii=False),
                        },
                    }
                ],
                "metadata": {
                    "requested_model": policy["model"],
                    "returned_model": policy["model"],
                    "adapter_version": "fake-rubric-agent-v1",
                    "usage": {},
                    "prompt_hash": digest(body),
                },
            }
        )


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


def build_model(settings, policy):
    return DeepSeekAgentModel(settings) if policy["provider"] == "deepseek" else FakeAgentModel()

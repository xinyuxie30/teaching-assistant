"""Tool protocol contracts against a mock provider; no external model calls."""

import json

import httpx
import pytest
from teaching_assistant.agent.context import initial_messages, manifest, payload, tool_result
from teaching_assistant.agent.contracts import RunInput, ToolCall
from teaching_assistant.agent.models import DeepSeekAgentModel
from teaching_assistant.agent.tools import validate_call
from teaching_assistant.domain import DomainError, ModelError
from test_deepseek import response_body, settings


def tool_response(*, arguments="{}", call_id="tool-1"):
    return response_body(
        choices=[
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": "read_question", "arguments": arguments},
                        }
                    ],
                },
            }
        ]
    )


async def invoke(response, captured):
    config = settings()
    policy = manifest(config, "deepseek-rubric-agent-v1")
    options = RunInput()
    body = payload(initial_messages(options.instructions), policy, options.model_dump())

    def provider(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=response, headers={"x-request-id": "agent-receipt"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        return await DeepSeekAgentModel(config, client=client).respond(body, policy)


async def test_deepseek_tool_protocol_roundtrip_and_receipt():
    requests = []
    response = await invoke(tool_response(), requests)
    assert len(requests) == 1
    request = requests[0]
    assert request["thinking"] == {"type": "disabled"} and request["tool_choice"] == "auto"
    assert {tool["function"]["name"] for tool in request["tools"]} == {
        "read_question",
        "propose_rubric",
    }
    assert "response_format" not in request
    call = response.tool_calls[0]
    validate_call(call)
    result = tool_result(call.id, {"ok": True, "result": {"question": "原文"}})
    assert response.message()["tool_calls"][0]["id"] == result["tool_call_id"] == "tool-1"
    assert response.metadata.provider_fingerprint == "fixture-revision"
    assert response.metadata.provider_request_id == "agent-receipt"
    assert response.metadata.usage["input_tokens"] == 10


async def test_controller_insufficient_balance_is_terminal_and_keeps_receipt():
    config = settings()
    policy = manifest(config, "deepseek-rubric-agent-v1")
    options = RunInput()
    body = payload(initial_messages(options.instructions), policy, options.model_dump())
    calls = []

    def provider(request):
        calls.append(request)
        return httpx.Response(
            402,
            json={"error": {"message": "private insufficient balance diagnostic"}},
            headers={"x-request-id": "balance-receipt", "Retry-After": "7"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        with pytest.raises(ModelError) as raised:
            await DeepSeekAgentModel(config, client=client).respond(body, policy)
    failure = raised.value
    assert failure.code == "provider_insufficient_balance"
    assert failure.retryable is False
    assert failure.http_status == 402
    assert failure.provider_request_id == "balance-receipt"
    assert len(calls) == 1
    assert "private" not in str(failure)


async def test_provider_tool_index_is_ignored_not_rejected():
    """DeepSeek adds ``index`` to each tool call; it is transport metadata, not contract data."""
    body = tool_response()
    body["choices"][0]["message"]["tool_calls"][0]["index"] = 0
    response = await invoke(body, [])
    assert response.tool_calls[0].id == "tool-1"
    assert "index" not in response.message()["tool_calls"][0]


@pytest.mark.parametrize(
    "case,code",
    [
        ("duplicate_ids", "provider_invalid_response"),
        ("wrong_finish_reason", "provider_invalid_response"),
        ("wrong_model", "provider_model_version_mismatch"),
        ("truncated", "provider_incomplete_response"),
        ("invalid_calls", "provider_invalid_response"),
    ],
)
async def test_invalid_tool_reply_preserves_metadata_without_hidden_retry(case, code):
    body = tool_response()
    choice = body["choices"][0]
    if case == "duplicate_ids":
        choice["message"]["tool_calls"] *= 2
    elif case == "wrong_finish_reason":
        choice["finish_reason"] = "stop"
    elif case == "wrong_model":
        body["model"] = "unexpected-provider-alias"
    elif case == "truncated":
        choice["finish_reason"] = "length"
    else:
        choice["finish_reason"] = "stop"
        choice["message"]["tool_calls"] = {}
    requests = []
    with pytest.raises(ModelError) as error:
        await invoke(body, requests)
    assert error.value.code == code and len(requests) == 1
    assert error.value.provider_fingerprint == "fixture-revision"
    assert error.value.provider_request_id == "agent-receipt"
    assert error.value.usage["input_tokens"] == 10


@pytest.mark.parametrize(
    "arguments", ['{"teacher_id":"attacker"}', '{"x":1,"x":2}', '{"x":NaN}', "[]"]
)
async def test_model_arguments_are_untrusted_until_registry_validation(arguments):
    response = await invoke(tool_response(arguments=arguments), [])
    with pytest.raises(DomainError) as error:
        validate_call(response.tool_calls[0])
    assert error.value.code == "invalid_tool_arguments"


def test_descending_levels_return_an_actionable_reason():
    """A real model ordered levels top-down; the tool must explain the ordering rule."""
    rubric = {
        "rubric": {
            "global_instructions": "测试",
            "criteria": [
                {
                    "id": "c1",
                    "description": "测试评分点",
                    "maximum_points": 5,
                    "levels": [
                        {"id": "high", "ordinal": 0, "description": "最高", "points": 5},
                        {"id": "low", "ordinal": 1, "description": "最低", "points": 0},
                    ],
                }
            ],
        }
    }
    call = ToolCall.model_validate(
        {
            "id": "tool-order",
            "function": {
                "name": "propose_rubric",
                "arguments": json.dumps(rubric, ensure_ascii=False),
            },
        }
    )
    with pytest.raises(DomainError) as error:
        validate_call(call)
    assert error.value.code == "invalid_tool_arguments"
    assert "monotonic" in error.value.detail

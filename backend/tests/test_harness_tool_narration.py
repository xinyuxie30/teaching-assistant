"""Versioned summaries preserve complete tool protocol and authoritative results."""

import json
from copy import deepcopy

import pytest
from teaching_assistant.domain import ModelError, digest
from teaching_assistant.harness.context import tool_result
from teaching_assistant.harness.semantic_compaction import (
    SEMANTIC_TOOL_VERSION,
    SEMANTIC_VERSION,
    SummaryRequest,
    accept_context_summary,
    fake_summary_reply,
    plan_context,
)


def exchange(index, content=None):
    return [
        {
            "role": "assistant",
            "content": content,
            "tool_calls": [
                {
                    "id": f"read-{index}",
                    "type": "function",
                    "function": {"name": "read_facts", "arguments": f'{{"page":{index}}}'},
                }
            ],
        },
        tool_result(f"read-{index}", {"ok": True, "result": {"fixed_fact": str(index)}}),
    ]


def source():
    return [
        {"role": "system", "content": "Preserve the original facts."},
        {"role": "user", "content": "Create a reviewable draft."},
        *exchange(0, "Tentative historical reasoning. " * 120),
        *(message for index in range(1, 5) for message in exchange(index)),
    ]


def options(version=SEMANTIC_TOOL_VERSION, **overrides):
    result = {
        "policy": {"model": "fake", "context_version": version},
        "character_limit": 6000,
        "measure": lambda messages: (
            len(json.dumps(messages, ensure_ascii=False, separators=(",", ":"))) + 1800
        ),
        "read_tools": {"read_facts"},
    }
    result.update(overrides)
    return result


def compact(messages, **overrides):
    config = options(**overrides)
    request = plan_context(messages, **config)
    assert isinstance(request, SummaryRequest)
    reply = fake_summary_reply(request.body, config["policy"])
    return request, accept_context_summary(messages, plan=request.plan, reply=reply, **config)


def test_tool_narration_summary_keeps_calls_parameters_and_receipts_exact():
    messages = source()
    original = deepcopy(messages)
    request, result = compact(messages)
    assert request.plan["version"] == SEMANTIC_TOOL_VERSION
    assert request.plan["sources"] == [{"source_index": 2, "source_hash": digest(messages[2])}]
    summary_input = json.loads(request.body["messages"][1]["content"])
    assert summary_input["sources"][0]["content"] == messages[2]["content"]
    assert "fixed_fact" not in json.dumps(summary_input)
    assert result.messages[2]["tool_calls"] == messages[2]["tool_calls"]
    assert result.messages[:2] == messages[:2]
    assert result.messages[3:] == messages[3:]
    assert result.checkpoint["after_characters"] < 6000
    assert messages == original
    assert plan_context(messages, **options(checkpoint=result.checkpoint)) == result


def test_legacy_version_never_summarizes_tool_narration():
    with pytest.raises(ModelError) as failure:
        plan_context(source(), **options(version=SEMANTIC_VERSION))
    assert failure.value.code == "agent_context_budget_exceeded"


@pytest.mark.parametrize("protection", ["incomplete", "recent"])
def test_incomplete_and_recent_tool_narration_are_protected(protection):
    messages = source()
    if protection == "incomplete":
        del messages[3]
    else:
        messages[2]["content"], messages[-2]["content"] = (
            messages[-2]["content"],
            messages[2]["content"],
        )
    with pytest.raises(ModelError) as failure:
        plan_context(messages, **options())
    assert failure.value.code == "agent_context_budget_exceeded"


@pytest.mark.parametrize("change", ["version", "parameters", "result"])
def test_tool_summary_checkpoint_rejects_changed_protocol_or_policy(change):
    messages = source()
    _, result = compact(messages)
    config = options(checkpoint=result.checkpoint)
    if change == "version":
        config["policy"]["context_version"] = SEMANTIC_VERSION
    elif change == "parameters":
        messages[2]["tool_calls"][0]["function"]["arguments"] = '{"page":99}'
    else:
        messages[3]["content"] = json.dumps({"ok": True, "result": "changed"})
    with pytest.raises(ModelError) as failure:
        plan_context(messages, **config)
    assert failure.value.code == "agent_context_checkpoint_mismatch"


def test_summary_reply_cannot_downgrade_version():
    messages = source()
    config = options()
    request = plan_context(messages, **config)
    reply = fake_summary_reply(request.body, config["policy"])
    content = json.loads(reply.content)
    content["version"] = SEMANTIC_VERSION
    reply.content = json.dumps(content)
    with pytest.raises(ModelError) as failure:
        accept_context_summary(messages, plan=request.plan, reply=reply, **config)
    assert failure.value.code == "agent_context_summary_invalid"

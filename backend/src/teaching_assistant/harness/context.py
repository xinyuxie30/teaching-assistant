"""Exact context assembly and request budgeting, independent of task and provider."""

import json

from teaching_assistant.domain import ModelError


def initial_messages(system, instructions):
    return [{"role": "system", "content": system}, {"role": "user", "content": instructions}]


def build_payload(messages, policy, schemas):
    """Build the exact provider request before context projection or budgeting."""
    return {
        "model": policy["model"],
        "messages": messages,
        "tools": schemas,
        "tool_choice": "auto",
        "thinking": {"type": "disabled"},
        "stream": False,
        "max_tokens": policy.get("max_output_tokens", 2048),
    }


def payload(messages, policy, limits, schemas):
    body = build_payload(messages, policy, schemas)
    limit = min(limits["max_context_characters"], policy["max_input_characters"])
    if len(json.dumps(body, ensure_ascii=False, separators=(",", ":"))) > limit:
        raise ModelError("agent_context_budget_exceeded", retryable=False)
    return body


def tool_result(call_id, body):
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": json.dumps(body, ensure_ascii=False, separators=(",", ":")),
    }

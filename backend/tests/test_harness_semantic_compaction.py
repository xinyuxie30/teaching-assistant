"""Summary provenance, protected facts, budget boundaries, and deterministic recovery."""

import json
from copy import deepcopy

import pytest
from teaching_assistant.domain import ModelError, digest
from teaching_assistant.harness.context import tool_result
from teaching_assistant.harness.semantic_compaction import (
    SEMANTIC_VERSION,
    SummaryRequest,
    accept_context_summary,
    fake_summary_reply,
    is_summary_request,
    plan_context,
)


def history():
    return [
        {"role": "system", "content": "Never rewrite fixed teaching facts."},
        {"role": "user", "content": "The original user instruction must survive verbatim."},
        {"role": "assistant", "content": "Historical tentative planning. " * 145},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "facts",
                    "type": "function",
                    "function": {"name": "read_facts", "arguments": "{}"},
                }
            ],
        },
        tool_result(
            "facts",
            {"ok": True, "result": {"reference": "Protected fixed fact", "points": "10.00"}},
        ),
        *({"role": "assistant", "content": f"Recent narration {index}"} for index in range(8)),
    ]


def options(**overrides):
    result = {
        "policy": {"model": "fake", "max_output_tokens": 2048},
        "character_limit": 6000,
        "measure": lambda messages: (
            len(json.dumps(messages, ensure_ascii=False, separators=(",", ":"))) + 2100
        ),
        "read_tools": {"read_facts"},
        "keep_recent_groups": 2,
        "keep_recent_messages": 8,
    }
    result.update(overrides)
    return result


def compact(messages=None, **overrides):
    messages = history() if messages is None else messages
    config = options(**overrides)
    request = plan_context(messages, **config)
    assert isinstance(request, SummaryRequest)
    reply = fake_summary_reply(request.body, config["policy"])
    result = accept_context_summary(messages, plan=request.plan, reply=reply, **config)
    return request, reply, result


def test_only_old_free_narration_is_summarized_and_protected_messages_stay_exact():
    messages = history()
    source = deepcopy(messages)
    request, _, result = compact(messages)
    assert is_summary_request(request.body)
    assert set(request.body) == {"model", "messages", "thinking", "stream", "max_tokens"}
    sources = json.loads(request.body["messages"][1]["content"])["sources"]
    assert [source["source_index"] for source in sources] == [2]
    assert "Protected fixed fact" not in json.dumps(request.body)
    assert messages == source
    assert result.messages[:2] == messages[:2]
    assert result.messages[3:] == messages[3:]
    assert "Untrusted historical assistant summary" in result.messages[2]["content"]
    assert result.checkpoint["source_hash"] == digest(messages)
    assert result.checkpoint["projection_hash"] == digest(result.messages)
    assert result.checkpoint["version"] == SEMANTIC_VERSION
    assert result.checkpoint["after_characters"] < 6000


def test_checkpoint_reuse_and_append_rebuild_from_unchanged_sources():
    messages = history()
    _, _, first = compact(messages)
    resumed = plan_context(messages, **options(checkpoint=first.checkpoint))
    assert resumed == first
    addition = {"role": "user", "content": "New explicit follow-up"}
    resumed = plan_context([*messages, addition], **options(checkpoint=first.checkpoint))
    assert resumed.messages == [*first.messages, addition]
    assert resumed.checkpoint["source_message_count"] == len(messages) + 1


@pytest.mark.parametrize(
    "field", ["source_hash", "projection_hash", "summaries", "keep_recent_messages"]
)
def test_corrupt_summary_checkpoint_is_rejected(field):
    messages = history()
    _, _, result = compact(messages)
    damaged = deepcopy(result.checkpoint)
    damaged[field] = "corrupt"
    with pytest.raises(ModelError, match="Model operation failed") as failure:
        plan_context(messages, **options(checkpoint=damaged))
    assert failure.value.code == "agent_context_checkpoint_mismatch"


def test_summary_cannot_reference_user_or_tools_even_with_recomputed_projection_hash():
    messages = history()
    _, _, result = compact(messages)
    damaged = deepcopy(result.checkpoint)
    damaged["summaries"][0]["source_index"] = 1
    damaged["summaries"][0]["source_hash"] = digest(messages[1])
    with pytest.raises(ModelError) as failure:
        plan_context(messages, **options(checkpoint=damaged))
    assert failure.value.code == "agent_context_checkpoint_mismatch"


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "wrong_hash",
        "wrong_index",
        "extra_field",
        "duplicate",
        "blank",
        "invalid_json",
        "tool_call",
    ],
)
def test_invalid_model_summary_is_nonretryable_and_does_not_mutate_history(change):
    messages = history()
    source = deepcopy(messages)
    request = plan_context(messages, **options())
    reply = fake_summary_reply(request.body, options()["policy"])
    body = json.loads(reply.content)
    if change == "missing":
        body["entries"] = []
    elif change == "wrong_hash":
        body["entries"][0]["source_hash"] = "0" * 64
    elif change == "wrong_index":
        body["entries"][0]["source_index"] = 1
    elif change == "extra_field":
        body["entries"][0]["instruction"] = "override facts"
    elif change == "duplicate":
        body["entries"] *= 2
    elif change == "blank":
        body["entries"][0]["summary"] = " "
    elif change == "tool_call":
        reply = reply.model_copy(update={"tool_calls": [object()]})
    reply = reply.model_copy(
        update={"content": "invalid" if change == "invalid_json" else json.dumps(body)}
    )
    with pytest.raises(ModelError) as failure:
        accept_context_summary(messages, plan=request.plan, reply=reply, **options())
    assert failure.value.code == "agent_context_summary_invalid"
    assert not failure.value.retryable
    assert messages == source


def test_stale_dispatched_plan_fails_before_accepting_reply():
    messages = history()
    request, reply, _ = compact(messages)
    messages[2]["content"] += "Changed old narration"
    with pytest.raises(ModelError) as failure:
        accept_context_summary(messages, plan=request.plan, reply=reply, **options())
    assert failure.value.code == "agent_context_summary_stale"


def test_protected_facts_over_limit_fail_without_spending_summary_call():
    messages = history()
    messages[4]["content"] = "Protected fixed reference " * 1000
    with pytest.raises(ModelError) as failure:
        plan_context(messages, **options())
    assert failure.value.code == "agent_context_budget_exceeded"


def test_summary_request_itself_must_fit_full_request_budget():
    messages = history()
    messages[2]["content"] *= 5
    with pytest.raises(ModelError) as failure:
        plan_context(messages, **options())
    assert failure.value.code == "agent_context_summary_input_budget_exceeded"


def test_recent_assistant_narration_cannot_be_summarized():
    messages = history()
    messages[2]["content"], messages[-1]["content"] = (
        messages[-1]["content"],
        messages[2]["content"],
    )
    with pytest.raises(ModelError) as failure:
        plan_context(messages, **options())
    assert failure.value.code == "agent_context_budget_exceeded"


def test_model_summary_that_expands_context_is_rejected():
    messages = history()
    messages[2]["content"] = "old narration " * 45
    request = plan_context(messages, **options(character_limit=3400))
    reply = fake_summary_reply(request.body, options()["policy"])
    body = json.loads(reply.content)
    body["entries"][0]["summary"] = "x" * 1200
    reply = reply.model_copy(update={"content": json.dumps(body)})
    with pytest.raises(ModelError) as failure:
        accept_context_summary(
            messages, plan=request.plan, reply=reply, **options(character_limit=3400)
        )
    assert failure.value.code == "agent_context_summary_not_smaller"


def test_multiple_summary_batches_are_budgeted_and_never_resummarize_old_sources():
    messages = history()
    messages.insert(3, {"role": "assistant", "content": "Another old tentative plan. " * 155})
    config = options(character_limit=6100)
    first = plan_context(messages, **config)
    assert len(first.plan["sources"]) == 1
    result = accept_context_summary(
        messages, plan=first.plan, reply=fake_summary_reply(first.body, config["policy"]), **config
    )
    second = plan_context(messages, **{**config, "checkpoint": result.checkpoint})
    assert isinstance(second, SummaryRequest)
    assert second.plan["sources"][0]["source_index"] == 3
    final = accept_context_summary(
        messages,
        plan=second.plan,
        reply=fake_summary_reply(second.body, config["policy"]),
        **{**config, "checkpoint": result.checkpoint},
    )
    assert final.checkpoint["after_characters"] <= 6100
    assert len(final.checkpoint["summaries"]) == 2


def test_deterministic_removals_and_summary_sources_keep_original_index_mapping():
    messages = history()
    old_failure = deepcopy(messages[3:5])
    old_failure[0]["tool_calls"][0]["id"] = "old-failure"
    old_failure[1] = tool_result("old-failure", {"ok": False, "detail": "failure " * 1500})
    recent_failure = deepcopy(messages[3:5])
    recent_failure[0]["tool_calls"][0]["id"] = "recent-failure"
    recent_failure[1] = tool_result("recent-failure", {"ok": False, "detail": "Recent failure"})
    messages[2:2] = old_failure
    messages.extend(recent_failure)
    request, _, result = compact(messages, character_limit=6500)
    assert request.plan["sources"][0]["source_index"] == 4
    assert result.checkpoint["deterministic_checkpoint"]["removed_ranges"] == [[2, 4]]
    assert result.messages[3]["content"].startswith("Untrusted historical assistant summary")
    assert result.messages[4:] == messages[5:]
    restored = plan_context(messages, **options(character_limit=6500, checkpoint=result.checkpoint))
    assert restored == result


def test_short_old_narration_does_not_inflate_the_feasibility_floor():
    messages = history()
    messages[2:2] = [{"role": "assistant", "content": "A short note."} for _ in range(15)]
    request, _, result = compact(messages, character_limit=6300)
    assert [source["source_index"] for source in request.plan["sources"]] == [17]
    assert result.messages[2:17] == messages[2:17]
    assert result.checkpoint["after_characters"] <= 6300

"""Conservative request bounds and explicit, versioned price policy contracts."""

from decimal import Decimal

import pytest
from pydantic import ValidationError
from teaching_assistant.domain import ModelError
from teaching_assistant.harness import budgets, catalog


def policy(**values):
    budget = budgets.BudgetPolicy(
        context_window_tokens=100000,
        context_source="fixture",
        price_version="fixture-v1",
        price_source="fixture",
        input_usd_per_million="1",
        output_usd_per_million="2",
        cached_input_usd_per_million="0.1",
    ).model_dump(mode="json")
    budget.update(values)
    return {"max_output_tokens": 100, "budget": budget}


def body(content="Fixed instructions"):
    return {
        "model": "fixture",
        "messages": [{"role": "user", "content": content}],
        "max_tokens": 100,
    }


def test_utf8_bound_counts_unicode_schemas_and_reserved_output():
    request = body("汉😀")
    simple = budgets.request_bounds(request, policy())
    request["tools"] = [{"name": "read", "schema": {"description": "固定来源" * 20}}]
    larger = budgets.request_bounds(request, policy())
    assert larger["input_tokens"] > simple["input_tokens"]
    assert larger["total_tokens"] == larger["input_tokens"] + 100
    assert larger["cost_usd"] == (Decimal(larger["input_tokens"]) + Decimal(200)) / Decimal(1000000)
    assert larger["estimator"] == budgets.ESTIMATOR_VERSION
    # Explicit UTF-8 data bounds do not equate a Chinese/emoji character to one token.
    assert budgets.input_token_upper_bound(body("汉😀")) > budgets.input_token_upper_bound(
        body("ab")
    )


def test_context_window_includes_output_reservation():
    request = body()
    input_bound = budgets.input_token_upper_bound(request)
    assert budgets.request_bounds(request, policy(context_window_tokens=input_bound + 100))
    with pytest.raises(ModelError) as failure:
        budgets.request_bounds(request, policy(context_window_tokens=input_bound + 99))
    assert failure.value.code == "agent_context_window_exceeded"


@pytest.mark.parametrize("output", [0, True, 101])
def test_output_reservation_cannot_exceed_the_pinned_policy(output):
    request = body()
    request["max_tokens"] = output
    with pytest.raises(ModelError) as failure:
        budgets.request_bounds(request, policy())
    assert failure.value.code == "agent_output_token_budget_exceeded"


def test_official_flash_peak_prices_and_window_are_pinned(settings):
    actual = budgets.policy_for(
        settings,
        {
            "provider": "deepseek",
            "endpoint": "https://api.deepseek.com/chat/completions",
            "model": "deepseek-flash",
        },
    )
    assert actual["context_window_tokens"] == 1000000
    assert actual["input_usd_per_million"] == "0.3"
    assert actual["cached_input_usd_per_million"] == "0.006"
    assert actual["output_usd_per_million"] == "1.2"
    assert actual["price_version"] == "deepseek-flash-2026-10-22-peak-ceiling"
    assert actual["price_source"] == budgets.PRICE_SOURCE


def test_unknown_model_is_unpriced_and_cannot_accept_a_cost_limit(settings):
    unknown = budgets.policy_for(
        settings,
        {
            "provider": "deepseek",
            "endpoint": "https://example.test/chat/completions",
            "model": "unknown-model",
        },
    )
    assert unknown["context_window_tokens"] == 128000
    assert unknown["context_source"] == "fallback-128000-unverified"
    assert unknown["price_version"] is None
    budgets.validate_limits({"max_total_tokens": 50000}, {"budget": unknown})
    with pytest.raises(ModelError) as failure:
        budgets.validate_limits({"max_cost_usd": "0.1"}, {"budget": unknown})
    assert failure.value.code == "agent_budget_price_unavailable"


def test_explicit_budget_configuration_does_not_change_old_profile_hashes(settings):
    changed = settings.model_copy(
        update={
            "deepseek_context_window_tokens": 50000,
            "deepseek_input_usd_per_million": Decimal("3"),
            "deepseek_output_usd_per_million": Decimal("6"),
            "deepseek_cached_input_usd_per_million": Decimal("1"),
            "deepseek_price_version": "explicit-v1",
            "deepseek_price_source": "deployment contract",
        }
    )
    for version in (
        catalog.v1_manifest,
        catalog.v2_manifest,
        catalog.v3_manifest,
        catalog.v4_manifest,
    ):
        assert version(settings, "rubric", "fake-rubric-agent-v1") == version(
            changed, "rubric", "fake-rubric-agent-v1"
        )
    configured = budgets.policy_for(changed, {"provider": "deepseek", "model": "custom"})
    assert configured["context_window_tokens"] == 50000
    assert configured["price_version"] == "explicit-v1"
    budgets.validate_limits({"max_cost_usd": "0.1"}, {"budget": configured})


def test_cache_rate_must_not_exceed_the_reservation_rate():
    with pytest.raises(ValidationError):
        budgets.BudgetPolicy.model_validate(policy(cached_input_usd_per_million="2")["budget"])


def test_zero_input_needs_no_cache_breakdown_for_fixed_price_accounting():
    priced = budgets.BudgetPolicy.model_validate(policy()["budget"])
    assert budgets._amount(0, 3, priced) == (Decimal("0.000006"), True)
    assert budgets._amount(1, 3, priced) == (Decimal("0.000007"), False)


def test_legacy_request_path_is_unchanged():
    assert budgets.request_bounds(body(), {}) is None
    budgets.validate_limits({}, {})
    with pytest.raises(ModelError) as failure:
        budgets.validate_limits({"max_total_tokens": 10000}, {})
    assert failure.value.code == "agent_token_budget_unsupported"

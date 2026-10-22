"""Offline checks of the opt-in paid runner's independent safety gate."""

import asyncio
import importlib.util
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "verify_harness_real", Path(__file__).parents[1] / "tools" / "verify_harness_real.py"
)
runner = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(runner)


def policy():
    return {
        "provider": "deepseek",
        "model": runner.MODEL,
        "expected_returned_model": runner.MODEL,
        "endpoint": "https://api.deepseek.com/chat/completions",
        "max_output_tokens": 2048,
        "budget": runner.budgets.BudgetPolicy(
            context_window_tokens=1000000,
            context_source="offline-safety-check",
            price_version=runner.PRICE_VERSION,
            price_source=runner.budgets.PRICE_SOURCE,
            input_usd_per_million=runner.INPUT_PRICE,
            output_usd_per_million=runner.OUTPUT_PRICE,
            cached_input_usd_per_million=runner.CACHE_PRICE,
        ).model_dump(mode="json"),
    }


BODY = {"messages": [{"role": "user", "content": "Offline fixture"}], "max_tokens": 2048}


def reply(usage):
    return SimpleNamespace(metadata=SimpleNamespace(model_dump=lambda **_: {"usage": usage}))


async def test_success_settles_but_unknown_and_failed_calls_keep_reservation():
    guard = runner.CallGuard(4, Decimal("0.30"))
    first = await guard.reserve(uuid4(), BODY, policy())
    before = guard.accounted
    await guard.success(first, reply({"input_tokens": 10, "output_tokens": 5}))
    assert guard.accounted < before and first["usage_complete"]
    for failure in (False, True):
        item = await guard.reserve(uuid4(), BODY, policy())
        before = guard.accounted
        if failure:
            await guard.failure(item, TimeoutError())
        else:
            await guard.success(item, reply({}))
        assert guard.accounted == before and not item["usage_complete"]


async def test_call_cap_denies_even_when_first_call_cost_was_small():
    guard = runner.CallGuard(1, Decimal("0.30"))
    item = await guard.reserve(uuid4(), BODY, policy())
    await guard.success(item, reply({"input_tokens": 0, "output_tokens": 0}))
    with pytest.raises(runner.ModelError, match="Model operation failed") as caught:
        await guard.reserve(uuid4(), BODY, policy())
    assert caught.value.code == "real_verification_call_cap"
    assert len(guard.calls) == 1


async def test_concurrent_reservations_cannot_spend_same_budget_twice():
    probe = runner.CallGuard(10, Decimal("0.30"))
    item = await probe.reserve(uuid4(), BODY, policy())
    guard = runner.CallGuard(10, Decimal(item["reserved_cost_usd"]) * Decimal("1.5"))
    values = await asyncio.gather(
        *(guard.reserve(uuid4(), BODY, policy()) for _ in range(3)),
        return_exceptions=True,
    )
    assert sum(isinstance(value, dict) for value in values) == 1
    assert len(guard.calls) == 1 and len(guard.denials) == 2
    assert guard.accounted <= guard.max_cost_usd


async def test_gate_rejects_nonofficial_endpoint_before_reserving():
    guard = runner.CallGuard(1, Decimal("0.30"))
    with pytest.raises(runner.VerificationError, match="unexpected_model_endpoint"):
        await guard.reserve(
            uuid4(), BODY, {**policy(), "endpoint": "https://example.com/chat/completions"}
        )
    assert not guard.calls and not guard.accounted


@pytest.mark.parametrize("value", ["nan", "Infinity", "-1", "1.01"])
def test_cost_argument_rejects_nonfinite_or_unsafe_values(value):
    with pytest.raises(runner.argparse.ArgumentTypeError):
        runner.cost_limit(value)

"""Root-locked token/cost reservations with immutable, provider-reported settlements.

UTF-8 request size plus explicit protocol overhead is a conservative estimator,
not a provider tokenizer. Unknown responses retain their full reservation. Prices
are pinned accounting ceilings; even exact reported usage is not a billing receipt.
"""

import json
from decimal import ROUND_CEILING, Decimal
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, ValidationError, model_validator
from sqlalchemy import func, select

from teaching_assistant.comparison_costs import Pricing
from teaching_assistant.db import AgentModelReservation, AgentRun
from teaching_assistant.domain import Contract, ModelError, digest

BUDGET_VERSION = "token-cost-reservations-v1"
ESTIMATOR_VERSION = "utf8-json-envelope-v1"
PRICE_SOURCE = "https://api-docs.deepseek.com/quick_start/pricing/"
_MILLION = Decimal(1000000)
_COST_QUANTUM = Decimal("0.000000000001")


class BudgetPolicy(Contract):
    version: Literal["token-cost-reservations-v1"] = BUDGET_VERSION
    estimator: Literal["utf8-json-envelope-v1"] = ESTIMATOR_VERSION
    context_window_tokens: int = Field(ge=1024, le=10000000, strict=True)
    context_source: str = Field(min_length=1, max_length=2000)
    price_version: str | None = Field(default=None, min_length=1, max_length=120)
    price_source: str | None = Field(default=None, min_length=1, max_length=2000)
    input_usd_per_million: Decimal | None = Field(
        default=None, ge=0, le=100000, allow_inf_nan=False
    )
    output_usd_per_million: Decimal | None = Field(
        default=None, ge=0, le=100000, allow_inf_nan=False
    )
    cached_input_usd_per_million: Decimal | None = Field(
        default=None, ge=0, le=100000, allow_inf_nan=False
    )

    @model_validator(mode="after")
    def complete_pricing(self):
        values = (self.input_usd_per_million, self.output_usd_per_million)
        if any(value is not None for value in values):
            if (
                any(value is None for value in values)
                or not self.price_version
                or not self.price_source
            ):
                raise ValueError("Prices require both rates, version and source")
            Pricing(
                input_usd_per_million=self.input_usd_per_million,
                output_usd_per_million=self.output_usd_per_million,
                cached_input_usd_per_million=self.cached_input_usd_per_million,
                source=self.price_source,
            )
            if (
                self.cached_input_usd_per_million is not None
                and self.cached_input_usd_per_million > self.input_usd_per_million
            ):
                raise ValueError("Cache price exceeds the uncached reservation price")
        elif (
            self.price_version or self.price_source or self.cached_input_usd_per_million is not None
        ):
            raise ValueError("Price metadata requires rates")
        return self


def policy_for(settings, model_policy):
    """Build new policies only; callers preserve old v1-v4 manifest construction."""
    if model_policy["provider"] == "fake":
        return BudgetPolicy(
            context_window_tokens=1000000,
            context_source="deterministic-engineering-fixture",
            price_version="fake-zero-v1",
            price_source="deterministic-engineering-fixture",
            input_usd_per_million=0,
            output_usd_per_million=0,
            cached_input_usd_per_million=0,
        ).model_dump(mode="json")
    official = urlsplit(
        model_policy.get("endpoint", "")
    ).hostname == "api.deepseek.com" and model_policy.get(
        "expected_returned_model", model_policy.get("model")
    ) in {"deepseek-flash", "DeepSeek-V4.1-Flash-1017"}
    configured_window = settings.deepseek_context_window_tokens
    values = {
        "context_window_tokens": configured_window or (1000000 if official else 128000),
        "context_source": (
            "explicit-configuration"
            if configured_window is not None
            else PRICE_SOURCE
            if official
            else "fallback-128000-unverified"
        ),
    }
    if settings.deepseek_input_usd_per_million is not None:
        values.update(
            input_usd_per_million=settings.deepseek_input_usd_per_million,
            output_usd_per_million=settings.deepseek_output_usd_per_million,
            cached_input_usd_per_million=settings.deepseek_cached_input_usd_per_million,
            price_version=settings.deepseek_price_version,
            price_source=settings.deepseek_price_source,
        )
    elif official:
        values.update(
            input_usd_per_million="0.3",
            output_usd_per_million="1.2",
            cached_input_usd_per_million="0.006",
            price_version="deepseek-flash-2026-10-22-peak-ceiling",
            price_source=PRICE_SOURCE,
        )
    return BudgetPolicy.model_validate(values).model_dump(mode="json")


def _policy(manifest):
    value = manifest.get("budget")
    if value is None:
        return None
    try:
        return BudgetPolicy.model_validate(value)
    except (ValidationError, TypeError, ValueError):
        raise ModelError("agent_budget_policy_invalid", retryable=False) from None


def validate_limits(limits, manifest):
    """Validate optional teacher ceilings without changing old-policy semantics."""
    policy = _policy(manifest)
    tokens, cost = limits.get("max_total_tokens"), limits.get("max_cost_usd")
    if tokens is not None and (type(tokens) is not int or tokens <= 0):
        raise ModelError("agent_token_limit_invalid", retryable=False)
    if cost is not None:
        try:
            amount = Decimal(str(cost))
            if not amount.is_finite() or amount <= 0:
                raise ValueError
        except (ValueError, ArithmeticError):
            raise ModelError("agent_cost_limit_invalid", retryable=False) from None
        if policy is None or policy.input_usd_per_million is None:
            raise ModelError("agent_budget_price_unavailable", retryable=False)
    if tokens is not None and policy is None:
        raise ModelError("agent_token_budget_unsupported", retryable=False)


def input_token_upper_bound(body):
    """Bound decoded UTF-8 request data plus generous, versioned framing overhead.

    This includes schemas and tool arguments and deliberately does not use a
    characters-per-token ratio. Provider-reported overruns remain visible and
    terminate further work; the estimate is not advertised as exact tokenization.
    """
    try:
        if not isinstance(body, dict):
            raise ValueError
        encoded = json.dumps(
            body, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        messages, tools = body.get("messages", []), body.get("tools", [])
        if not isinstance(messages, list) or not isinstance(tools, list):
            raise ValueError
        return len(encoded) + 1024 + 128 * len(messages) + 512 * len(tools)
    except (ValueError, TypeError, UnicodeError, OverflowError):
        raise ModelError("agent_budget_request_invalid", retryable=False) from None


def _amount(input_tokens, output_tokens, policy, *, cached_tokens=None):
    if policy.input_usd_per_million is None:
        return None, None
    exact = True
    input_cost = Decimal(input_tokens) * policy.input_usd_per_million
    if policy.cached_input_usd_per_million is not None:
        if cached_tokens is None:
            exact = (
                input_tokens == 0
                or policy.cached_input_usd_per_million == policy.input_usd_per_million
            )
        else:
            input_cost = (
                Decimal(input_tokens - cached_tokens) * policy.input_usd_per_million
                + Decimal(cached_tokens) * policy.cached_input_usd_per_million
            )
    cost = (input_cost + Decimal(output_tokens) * policy.output_usd_per_million) / _MILLION
    bounded = cost.quantize(_COST_QUANTUM, rounding=ROUND_CEILING)
    return bounded, exact and bounded == cost


def request_bounds(body, manifest):
    policy = _policy(manifest)
    if policy is None:
        return None
    output = body.get("max_tokens")
    if type(output) is not int or output <= 0 or output > manifest.get("max_output_tokens", 2048):
        raise ModelError("agent_output_token_budget_exceeded", retryable=False)
    input_tokens = input_token_upper_bound(body)
    if input_tokens + output > policy.context_window_tokens:
        raise ModelError("agent_context_window_exceeded", retryable=False)
    cost, _ = _amount(input_tokens, output, policy)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output,
        "total_tokens": input_tokens + output,
        "cost_usd": cost,
        "estimator": policy.estimator,
    }


async def _locked_root(session, run):
    root = await session.scalar(
        select(AgentRun).where(AgentRun.id == (run.root_run_id or run.id)).with_for_update()
    )
    if root is None or root.parent_run_id is not None or root.teacher_id != run.teacher_id:
        raise ModelError("invalid_agent_tree", retryable=False)
    return root


async def report(session, root):
    rows = list(
        await session.scalars(
            select(AgentModelReservation).where(AgentModelReservation.root_run_id == root.id)
        )
    )
    reported_tokens = reserved_tokens = 0
    known_cost = reserved_cost = accounted_cost = Decimal(0)
    unknown_cost_count = unsettled_count = 0
    for row in rows:
        if row.settled_at is None:
            unsettled_count += 1
            reserved_tokens += row.reserved_input_tokens + row.reserved_output_tokens
            amount = row.reserved_cost_usd
            if amount is not None:
                reserved_cost += amount
        else:
            reported_tokens += row.actual_input_tokens + row.actual_output_tokens
            amount = row.settled_cost_usd
            if row.cost_is_exact:
                known_cost += amount
        if amount is None:
            unknown_cost_count += 1
        else:
            accounted_cost += amount
    return {
        "version": BUDGET_VERSION,
        "reported_tokens": reported_tokens,
        "reserved_tokens": reserved_tokens,
        "accounted_tokens": reported_tokens + reserved_tokens,
        "known_cost_usd": str(known_cost),
        "reserved_cost_usd": str(reserved_cost),
        "accounted_cost_usd": str(accounted_cost) if not unknown_cost_count else None,
        "unpriced_calls": unknown_cost_count,
        "unsettled_calls": unsettled_count,
        "calls": len(rows),
        "prices_are_accounting_ceilings": True,
    }


def _over_limit(root, totals, *, extra_tokens=0, extra_cost=None):
    token_limit, cost_limit = root.limits.get("max_total_tokens"), root.limits.get("max_cost_usd")
    if token_limit is not None and totals["accounted_tokens"] + extra_tokens > token_limit:
        return "agent_token_budget_exhausted"
    if cost_limit is not None:
        if totals["accounted_cost_usd"] is None or extra_cost is None:
            return "agent_budget_price_unavailable"
        if Decimal(totals["accounted_cost_usd"]) + extra_cost > Decimal(str(cost_limit)):
            return "agent_cost_budget_exhausted"
    return None


async def reserve(session, run, dispatch_sequence, body):
    """Reserve atomically with model_dispatched; the caller already owns the root lock."""
    policy = _policy(run.manifest)
    if policy is None:
        return None
    root = await _locked_root(session, run)
    validate_limits(root.limits, root.manifest)
    bounds = request_bounds(body, run.manifest)
    fixed_policy = policy.model_dump(mode="json")
    existing = await session.scalar(
        select(AgentModelReservation).where(
            AgentModelReservation.run_id == run.id,
            AgentModelReservation.dispatch_sequence == dispatch_sequence,
        )
    )
    if existing is not None:
        if (
            existing.root_run_id != root.id
            or existing.request_hash != digest(body)
            or existing.policy_hash != digest(fixed_policy)
        ):
            raise ModelError("agent_budget_reservation_conflict", retryable=False)
        return existing
    totals = await report(session, root)
    violation = _over_limit(
        root, totals, extra_tokens=bounds["total_tokens"], extra_cost=bounds["cost_usd"]
    )
    if violation:
        raise ModelError(violation, retryable=False)
    row = AgentModelReservation(
        root_run_id=root.id,
        run_id=run.id,
        dispatch_sequence=dispatch_sequence,
        request_hash=digest(body),
        policy_hash=digest(fixed_policy),
        policy=fixed_policy,
        reserved_input_tokens=bounds["input_tokens"],
        reserved_output_tokens=bounds["output_tokens"],
        reserved_cost_usd=bounds["cost_usd"],
    )
    session.add(row)
    await session.flush()
    return row


def _usage(usage):
    if not isinstance(usage, dict) or not {"input_tokens", "output_tokens"} <= usage.keys():
        return None
    if any(type(value) is not int or value < 0 for value in usage.values()):
        return None
    hit, miss = usage.get("input_cached_tokens"), usage.get("input_uncached_tokens")
    if (hit is not None or miss is not None) and (
        hit is None or miss is None or hit + miss != usage["input_tokens"]
    ):
        return None
    return dict(usage)


async def settle(session, run, dispatch_sequence, usage):
    """Record known usage once; return a violation for the caller to stop after accounting.

    Unknown/incomplete usage does not release any reserved capacity. A settled
    receipt cannot be changed, including by a retry or a late different response.
    """
    if _policy(run.manifest) is None:
        return None
    root = await _locked_root(session, run)
    row = await session.scalar(
        select(AgentModelReservation).where(
            AgentModelReservation.run_id == run.id,
            AgentModelReservation.dispatch_sequence == dispatch_sequence,
        )
    )
    if row is None or row.root_run_id != root.id:
        raise ModelError("agent_budget_reservation_missing", retryable=False)
    if digest(row.policy) != row.policy_hash:
        raise ModelError("agent_budget_policy_invalid", retryable=False)
    known = _usage(usage)
    if known is None:
        return None
    if row.settled_at is not None:
        if row.usage != known:
            raise ModelError("agent_usage_receipt_conflict", retryable=False)
    else:
        policy = BudgetPolicy.model_validate(row.policy)
        row.actual_input_tokens = known["input_tokens"]
        row.actual_output_tokens = known["output_tokens"]
        row.settled_cost_usd, row.cost_is_exact = _amount(
            known["input_tokens"],
            known["output_tokens"],
            policy,
            cached_tokens=known.get("input_cached_tokens"),
        )
        row.usage = known
        row.settled_at = await session.scalar(select(func.clock_timestamp()))
        await session.flush()
    totals = await report(session, root)
    violation = _over_limit(root, totals, extra_cost=Decimal(0))
    if violation:
        return violation
    if (
        row.actual_input_tokens > row.reserved_input_tokens
        or row.actual_output_tokens > row.reserved_output_tokens
    ):
        return "agent_usage_exceeds_reservation"
    return None

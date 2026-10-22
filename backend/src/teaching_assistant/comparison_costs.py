"""Explicit, per-profile estimates; no bundled prices or zero-cost assumptions."""

import json
from decimal import Decimal
from pathlib import Path

from pydantic import Field

from teaching_assistant.domain import Contract


class Pricing(Contract):
    input_usd_per_million: Decimal = Field(ge=0, allow_inf_nan=False)
    output_usd_per_million: Decimal = Field(ge=0, allow_inf_nan=False)
    cached_input_usd_per_million: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    source: str = Field(min_length=1, max_length=2000)
    effective_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")


def resolve_pricing(args, profiles, registry, legacy):
    path = getattr(args, "pricing_file", None)
    real = [profile for profile in profiles if not profile.manifest["simulation"]]
    if path is None:
        if legacy is not None and len(real) != 1:
            raise ValueError("Global prices require exactly one real profile; use --pricing-file")
        return {real[0].id: legacy} if legacy is not None else {}
    if legacy is not None:
        raise ValueError("Use --pricing-file or global token prices, not both")
    raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict):
        raise ValueError("Pricing file must map profile aliases or IDs to pricing objects")
    selected = {profile.id for profile in real}
    resolved = {}
    for name, value in raw.items():
        profile = registry.resolve(name, require_ready=False)
        if profile.id not in selected or profile.id in resolved:
            raise ValueError("Pricing must reference selected real profiles exactly once")
        resolved[profile.id] = Pricing.model_validate(value).model_dump(mode="json")
    return resolved


def estimate_usage(usages: list[dict], pricing: dict | None) -> tuple[str | None, int]:
    if pricing is None:
        return None, len(usages)
    price = Pricing.model_validate(pricing)
    total, priced, unpriced = Decimal(0), 0, 0
    for usage in usages:
        if price.cached_input_usd_per_million is None:
            input_cost = Decimal(usage["input_tokens"]) * price.input_usd_per_million
        else:
            hit, miss = usage.get("input_cached_tokens"), usage.get("input_uncached_tokens")
            if hit is None or miss is None or hit + miss != usage["input_tokens"]:
                unpriced += 1
                continue
            input_cost = (
                Decimal(hit) * price.cached_input_usd_per_million
                + Decimal(miss) * price.input_usd_per_million
            )
        total += input_cost + Decimal(usage["output_tokens"]) * price.output_usd_per_million
        priced += 1
    return (str(total / Decimal(1_000_000)) if priced else None), unpriced

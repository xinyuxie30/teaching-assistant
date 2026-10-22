import asyncio
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from teaching_assistant.comparison import run_comparison
from teaching_assistant.comparison_costs import estimate_usage, resolve_pricing
from teaching_assistant.deepseek import DeepSeekEvaluator
from teaching_assistant.evaluators import ManualEvidenceExtractor
from teaching_assistant.profiles import ProfileRegistry
from test_deepseek import response_body, settings


def args_for(tmp_path, **overrides):
    args = {
        "dataset": str(Path(__file__).resolve().parents[2] / "evals/samples/synthetic-v1.json"),
        "profiles": ["deepseek-v1"],
        "dry_run": False,
        "output_dir": str(tmp_path / "reports"),
        "max_samples": 1,
        "max_calls": 1,
        "max_seconds": 10,
        "max_attempts": 1,
        "confidence_audit_threshold": 0.9,
        "severe_error_points": None,
        "input_usd_per_million": None,
        "output_usd_per_million": None,
        "price_source": None,
        "pricing_file": None,
        "cached_input_usd_per_million": None,
    }
    args.update(overrides)
    return SimpleNamespace(**args)


async def test_comparison_uses_deepseek_with_call_budget_and_keeps_gold_private(
    tmp_path, monkeypatch
):
    config = settings()
    sent = []

    def provider(request):
        payload = json.loads(request.content)
        sent.append(payload)
        rules = json.loads(
            payload["messages"][0]["content"].split("Teacher-confirmed rules (JSON):\n", 1)[1]
        )
        return httpx.Response(
            200,
            json=response_body(
                json.dumps(
                    {
                        "criterion_id": rules["criterion_id"],
                        "level_id": rules["levels"][0]["id"],
                    }
                )
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        monkeypatch.setattr(
            "teaching_assistant.comparison.build_adapters",
            lambda conf, profile: (
                DeepSeekEvaluator(conf, profile, client=client),
                ManualEvidenceExtractor(),
            ),
        )
        result = await run_comparison(config, args_for(tmp_path))
    reports = list((tmp_path / "reports").glob("*/report.json"))
    assert len(reports) == 1 and result
    report = json.loads(reports[0].read_text(encoding="utf-8"))
    assert len(sent) == 1 and "gold_levels" not in json.dumps(sent)
    assert report["evaluation_scope"] == "criterion_assessment_only"
    assert [row["status"] for row in report["results"]] == ["assessed", "budget_skipped"]
    assert report["results"][0]["evaluation"]["probabilities"] is None
    assert report["results"][0]["attempts"][0]["provider_fingerprint"] == "fixture-revision"


async def test_failed_comparison_keeps_provider_fingerprint(tmp_path, monkeypatch):
    config = settings()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=response_body(model="unexpected-model"))
        )
    ) as client:
        monkeypatch.setattr(
            "teaching_assistant.comparison.build_adapters",
            lambda conf, profile: (
                DeepSeekEvaluator(conf, profile, client=client),
                ManualEvidenceExtractor(),
            ),
        )
        result = await run_comparison(config, args_for(tmp_path))
    report = json.loads(await asyncio.to_thread(Path(result["report"]).read_text, encoding="utf-8"))
    failed = report["results"][0]
    assert failed["status"] == "failed"
    assert failed["error_code"] == "provider_model_version_mismatch"
    assert failed["attempts"][0]["provider_fingerprint"] == "fixture-revision"
    assert failed["attempts"][0]["usage"]["input_tokens"] == 10


def test_cache_prices_require_complete_cache_usage():
    pricing = {
        "input_usd_per_million": "2",
        "output_usd_per_million": "4",
        "cached_input_usd_per_million": "0.5",
        "source": "synthetic test tariff",
    }
    complete = {
        "input_tokens": 10,
        "output_tokens": 3,
        "input_cached_tokens": 4,
        "input_uncached_tokens": 6,
    }
    missing = {"input_tokens": 10, "output_tokens": 3}
    cost, missing_count = estimate_usage([complete, missing], pricing)
    assert Decimal(cost) == Decimal("0.000026") and missing_count == 1
    assert estimate_usage([missing], pricing) == (None, 1)
    assert estimate_usage([complete], None) == (None, 1)


def test_multi_provider_prices_are_resolved_separately(tmp_path):
    registry = ProfileRegistry(settings(jev_model="jev-1.14.0"))
    profiles = [registry.resolve(name, require_ready=False) for name in ["jev-v1", "deepseek-v1"]]
    price = {"input_usd_per_million": "2", "output_usd_per_million": "4", "source": "fixture"}
    with pytest.raises(ValueError, match="exactly one real profile"):
        resolve_pricing(args_for(tmp_path), profiles, registry, price)
    path = tmp_path / "prices.json"
    path.write_text(
        json.dumps(
            {
                "jev-v1": price,
                "deepseek-v1": {
                    **price,
                    "cached_input_usd_per_million": "0.5",
                    "input_usd_per_million": "3",
                },
            }
        ),
        encoding="utf-8",
    )
    resolved = resolve_pricing(args_for(tmp_path, pricing_file=str(path)), profiles, registry, None)
    assert resolved[profiles[0].id]["input_usd_per_million"] == "2"
    assert resolved[profiles[1].id]["input_usd_per_million"] == "3"
    assert resolved[profiles[1].id]["cached_input_usd_per_million"] == "0.5"

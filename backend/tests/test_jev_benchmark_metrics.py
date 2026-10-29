"""Prevent a benchmark from hiding missing predictions or exceeding its live-call cap."""

import importlib.util
from pathlib import Path

import pytest
from teaching_assistant.comparison import load_dataset
from teaching_assistant.domain import ModelError

SPEC = importlib.util.spec_from_file_location(
    "benchmark_jev_agent", Path(__file__).parents[1] / "tools/benchmark_jev_agent.py"
)
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


@pytest.mark.parametrize(
    "url",
    [
        "sqlite:///fixture_test",
        "postgresql+asyncpg://localhost/teaching",
        "postgresql+asyncpg://localhost/teaching_test_copy",
        "postgresql+asyncpg://localhost",
    ],
)
def test_benchmark_rejects_business_or_non_postgres_database(url):
    with pytest.raises(ValueError, match="separate PostgreSQL"):
        benchmark.checked_url(url)


async def test_live_request_cap_counts_unknown_outcomes_and_prevents_dispatch():
    class UnknownProvider:
        calls = 0

        async def assess(self, request):
            self.calls += 1
            raise ModelError("provider_timeout", retryable=True)

    adapter = UnknownProvider()
    budget = benchmark.ProviderBudget(2)
    bounded = benchmark.BoundedJev(adapter, budget)
    for _ in range(2):
        with pytest.raises(ModelError) as error:
            await bounded.assess(None)
        assert error.value.code == "provider_timeout"
    with pytest.raises(ModelError) as error:
        await bounded.assess(None)
    assert error.value.code == "benchmark_call_budget_exhausted"
    assert budget.used == adapter.calls == 2


def test_quality_denominator_includes_failed_and_abstained_predictions():
    dataset = load_dataset(Path(__file__).parents[2] / "evals/samples/synthetic-v1.json")
    sample = dataset.samples[0]
    result = benchmark.label_metrics(
        dataset,
        [
            ({"sample": sample}, sample.gold_levels),
            ({"sample": sample}, {}),
            ({"sample": sample}, {key: None for key in sample.gold_levels}),
        ],
        live=True,
    )
    assert result["point_accuracy_on_answered"] == 1
    assert result["point_coverage"] == result["end_to_end_correct_point_rate"] == 1 / 3
    assert result["complete_answer_count"] == 1
    assert result["independent_sample_count"] == 1 and result["answer_observations"] == 3


def test_stub_never_produces_a_semantic_quality_result():
    dataset = load_dataset(Path(__file__).parents[2] / "evals/samples/synthetic-v1.json")
    assert benchmark.label_metrics(dataset, [], live=False) is None


def test_empty_latency_is_unknown_instead_of_zero():
    assert all(value is None for value in benchmark.percentiles([]).values())

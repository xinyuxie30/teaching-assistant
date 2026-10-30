"""Protect paid-call reservations, score-band decoding and cross-question audits."""

import importlib.util
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from teaching_assistant.api import create_app
from teaching_assistant.cli import bootstrap
from teaching_assistant.comparison import Dataset, load_dataset
from teaching_assistant.db import AgentEvent, AgentRun
from teaching_assistant.domain import Criterion, ModelError

TOOLS = Path(__file__).parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
try:
    SPEC = importlib.util.spec_from_file_location(
        "benchmark_public_grading", TOOLS / "benchmark_public_grading.py"
    )
    runner = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(runner)
    import benchmark_jev_agent as benchmark
finally:
    sys.path.remove(str(TOOLS))


def criterion(identifier, maximum):
    return Criterion.model_validate(
        {
            "id": identifier,
            "description": "Cumulative score band",
            "maximum_points": maximum,
            "levels": [
                {"id": f"s{score}", "ordinal": score, "description": str(score), "points": score}
                for score in range(maximum + 1)
            ],
        }
    )


def test_budget_reopen_preserves_reservations_and_enforces_limit(tmp_path):
    path = tmp_path / "requests.jsonl"
    first = runner.DurableBudget(path, 2)
    first.reserve()  # A dispatch may fail or have an unknown outcome after this point.
    reopened = runner.DurableBudget(path, 2)
    assert reopened.used == 1
    reopened.reserve()
    with pytest.raises(ModelError) as error:
        reopened.reserve()
    assert error.value.code == "benchmark_call_budget_exhausted"
    assert error.value.retryable is False
    assert reopened.used == runner.DurableBudget(path, 2).used == 2
    assert [json.loads(line)["sequence"] for line in path.read_text().splitlines()] == [1, 2]


def test_budget_reservation_survives_abrupt_child_process_exit(tmp_path):
    path = tmp_path / "requests.jsonl"
    script = "\n".join(
        [
            "import os, sys",
            "from pathlib import Path",
            "sys.path.insert(0, sys.argv[1])",
            "from benchmark_public_grading import DurableBudget",
            "DurableBudget(Path(sys.argv[2]), 1).reserve()",
            "os._exit(23)",
        ]
    )
    child = subprocess.run(
        [sys.executable, "-c", script, str(TOOLS), str(path)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert child.returncode == 23, child.stderr
    reopened = runner.DurableBudget(path, 1)
    assert reopened.used == 1
    with pytest.raises(ModelError) as error:
        reopened.reserve()
    assert error.value.code == "benchmark_call_budget_exhausted"


def test_budget_does_not_refund_written_reservation_after_fsync_failure(tmp_path, monkeypatch):
    path = tmp_path / "requests.jsonl"
    budget = runner.DurableBudget(path, 2)

    def interrupted(_descriptor):
        raise OSError("Simulated persistence acknowledgement failure")

    monkeypatch.setattr(runner.os, "fsync", interrupted)
    with pytest.raises(ModelError) as error:
        budget.reserve()
    assert error.value.code == "benchmark_request_ledger_unavailable"
    assert error.value.retryable is False
    assert budget.poisoned and budget.used == 1
    before = path.read_bytes()
    with pytest.raises(ModelError) as error:
        budget.reserve()
    assert error.value.code == "benchmark_request_ledger_unavailable"
    assert path.read_bytes() == before and budget.used == 1
    # The caller cannot assume the reservation disappeared after an unknown write.
    assert runner.DurableBudget(path, 2).used == 1


def test_budget_append_failure_permanently_blocks_current_instance(tmp_path, monkeypatch):
    path = tmp_path / "requests.jsonl"
    budget = runner.DurableBudget(path, 5)
    original_open = Path.open
    opened = 0

    def unavailable(self, *args, **kwargs):
        nonlocal opened
        if self == path:
            opened += 1
            raise OSError("Simulated unavailable ledger")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", unavailable)
    for _ in range(2):
        with pytest.raises(ModelError) as error:
            budget.reserve()
        assert error.value.code == "benchmark_request_ledger_unavailable"
        assert error.value.retryable is False
    assert budget.poisoned and budget.used == opened == 1


def test_budget_control_interrupt_also_blocks_reuse(tmp_path, monkeypatch):
    path = tmp_path / "requests.jsonl"
    budget = runner.DurableBudget(path, 5)

    def interrupted(_descriptor):
        raise KeyboardInterrupt

    monkeypatch.setattr(runner.os, "fsync", interrupted)
    with pytest.raises(KeyboardInterrupt):
        budget.reserve()
    assert budget.poisoned and budget.used == 1
    with pytest.raises(ModelError) as error:
        budget.reserve()
    assert error.value.code == "benchmark_request_ledger_unavailable"


def test_budget_rejects_a_cap_lower_than_persisted_reservations(tmp_path):
    path = tmp_path / "requests.jsonl"
    budget = runner.DurableBudget(path, 2)
    budget.reserve()
    budget.reserve()
    with pytest.raises(ValueError, match="below existing reservations"):
        runner.DurableBudget(path, 1)


@pytest.mark.parametrize("sequences", [[2], [1, 1], [1, 3], [0], [1, 2, 1]])
def test_budget_rejects_corrupt_reservation_sequence(tmp_path, sequences):
    path = tmp_path / "requests.jsonl"
    path.write_text("".join(json.dumps({"sequence": number}) + "\n" for number in sequences))
    with pytest.raises(ValueError, match="sequence is corrupt"):
        runner.DurableBudget(path, 10)


def test_budget_rejects_truncated_json_instead_of_resetting_count(tmp_path):
    path = tmp_path / "requests.jsonl"
    path.write_text('{"sequence": 1}\n{"sequence":')
    with pytest.raises(json.JSONDecodeError):
        runner.DurableBudget(path, 10)


def test_output_lock_excludes_other_instances_and_releases_on_exit(tmp_path):
    output = tmp_path / "run"
    with runner.OutputLock(output):
        with pytest.raises(ValueError, match="Another runner"):
            with runner.OutputLock(output):
                pytest.fail("A second writer entered the same output directory")
        with runner.OutputLock(tmp_path / "independent-run"):
            pass
    with runner.OutputLock(output):
        pass


def test_output_lock_releases_after_exception(tmp_path):
    with pytest.raises(RuntimeError, match="interrupted run"):
        with runner.OutputLock(tmp_path):
            raise RuntimeError("interrupted run")
    with runner.OutputLock(tmp_path):
        pass


def test_output_lock_rejects_another_process_then_releases_after_parent_exit(tmp_path):
    script = "\n".join(
        [
            "import sys",
            "from pathlib import Path",
            "sys.path.insert(0, sys.argv[1])",
            "from benchmark_public_grading import OutputLock",
            "try:",
            "    with OutputLock(Path(sys.argv[2])): pass",
            "except ValueError:",
            "    sys.exit(17)",
        ]
    )

    def try_child():
        return subprocess.run(
            [sys.executable, "-c", script, str(TOOLS), str(tmp_path)],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

    with runner.OutputLock(tmp_path):
        blocked = try_child()
    assert blocked.returncode == 17, blocked.stderr
    released = try_child()
    assert released.returncode == 0, released.stderr


@pytest.mark.parametrize("score", range(10))
def test_single_band_nine_point_scale_is_consistent(score):
    assert runner.band_consistent([criterion("band0", 9)], {"band0": f"s{score}"}) is True


@pytest.mark.parametrize(
    ("selected", "expected"),
    [
        ({"band0": "s0", "band1": "s0"}, True),
        ({"band0": "s8", "band1": "s0"}, True),
        ({"band0": "s9", "band1": "s0"}, True),
        ({"band0": "s9", "band1": "s1"}, True),
        ({"band0": "s8", "band1": "s1"}, False),
        ({"band0": "s0", "band1": "s1"}, False),
        ({"band0": "s9", "band1": None}, None),
        ({"band0": "s9"}, None),
        ({"band1": "s1"}, None),
        ({}, None),
    ],
)
def test_two_band_ten_point_scale_requires_a_full_prefix(selected, expected):
    assert (
        runner.band_consistent([criterion("band0", 9), criterion("band1", 1)], selected) is expected
    )


def test_later_nonzero_band_after_partial_prefix_is_inconsistent():
    criteria = [criterion("band0", 9), criterion("band1", 9), criterion("band2", 2)]
    assert runner.band_consistent(criteria, {"band0": "s9", "band1": "s4", "band2": "s0"})
    assert not runner.band_consistent(criteria, {"band0": "s9", "band1": "s4", "band2": "s1"})


def sampling_suite(specifications):
    original = load_dataset(Path(__file__).parents[2] / "evals/samples/synthetic-v1.json")
    suite = []
    for index, (dataset, subject, split, count) in enumerate(specifications):
        samples = [
            original.samples[0].model_copy(
                update={"sample_id": f"sample-{index}-{offset}", "answer_version_id": uuid4()}
            )
            for offset in range(count)
        ]
        suite.append(
            {
                "dataset": original.model_copy(update={"samples": samples}),
                "rows": [
                    {
                        "dataset": dataset,
                        "subject": subject,
                        "split": split,
                        "sample_id": sample.sample_id,
                        "gold_score": 0,
                        "predicted_score": None,
                    }
                    for sample in samples
                ],
            }
        )
    return suite


def test_balanced_sampler_rotates_datasets_subjects_and_splits_without_reordering():
    suite = sampling_suite(
        [
            ("sas-bench", "physics", "holdout", 5),
            ("sas-bench", "chinese", "holdout", 5),
            ("semeval", "scientsbank", "unseen_answers", 5),
            ("semeval", "scientsbank", "unseen_questions", 5),
            ("semeval", "beetle", "unseen_answers", 5),
            ("semeval", "beetle", "unseen_questions", 5),
        ]
    )
    selected = runner.select_samples(suite, 8, "balanced-v1")
    rows = [row for group in selected for row in group["rows"]]
    assert Counter(row["dataset"] for row in rows) == {"sas-bench": 4, "semeval": 4}
    assert Counter((row["dataset"], row["subject"], row["split"]) for row in rows) == {
        ("sas-bench", "physics", "holdout"): 2,
        ("sas-bench", "chinese", "holdout"): 2,
        ("semeval", "scientsbank", "unseen_answers"): 1,
        ("semeval", "scientsbank", "unseen_questions"): 1,
        ("semeval", "beetle", "unseen_answers"): 1,
        ("semeval", "beetle", "unseen_questions"): 1,
    }
    assert len({row["sample_id"] for row in rows}) == 8
    for group in selected:
        assert [sample.sample_id for sample in group["dataset"].samples] == [
            row["sample_id"] for row in group["rows"]
        ]
    assert all(len(group["rows"]) == len(group["dataset"].samples) == 5 for group in suite)
    repeated = runner.select_samples(suite, 8, "balanced-v1")
    assert [[row["sample_id"] for row in group["rows"]] for group in selected] == [
        [row["sample_id"] for row in group["rows"]] for group in repeated
    ]


def test_balanced_sampler_fills_from_available_dataset_after_other_is_exhausted():
    suite = sampling_suite([("a", "science", "test", 1), ("b", "science", "test", 6)])
    rows = [
        row for group in runner.select_samples(suite, 5, "balanced-v1") for row in group["rows"]
    ]
    assert Counter(row["dataset"] for row in rows) == {"a": 1, "b": 4}
    assert (
        sum(len(group["rows"]) for group in runner.select_samples(suite, 100, "balanced-v1")) == 7
    )


def test_sampler_never_reads_gold_labels_or_model_predictions():
    class InputMetadata(dict):
        def __getitem__(self, key):
            if key in {"gold_score", "predicted_score", "label_valid"}:
                pytest.fail("Sampling inspected a label or prediction")
            return super().__getitem__(key)

        def get(self, key, default=None):
            if key in {"gold_score", "predicted_score", "label_valid"}:
                pytest.fail("Sampling inspected a label or prediction")
            return super().get(key, default)

    suite = sampling_suite([("a", "science", "test", 3), ("b", "reading", "test", 3)])
    for group in suite:
        group["rows"] = [InputMetadata(row) for row in group["rows"]]
        for sample in group["dataset"].samples:
            # Neither metadata labels nor in-memory rubric labels select samples.
            sample.gold_levels = {"opaque-gold": object()}
    for policy in ("prefix", "balanced-v1"):
        selected = runner.select_samples(suite, 4, policy)
        assert sum(len(group["rows"]) for group in selected) == 4


def test_prefix_sampler_keeps_source_order_and_zero_means_full_suite():
    suite = sampling_suite([("a", "science", "test", 3), ("b", "reading", "test", 3)])
    selected = runner.select_samples(suite, 4, "prefix")
    assert [row["sample_id"] for group in selected for row in group["rows"]] == [
        "sample-0-0",
        "sample-0-1",
        "sample-0-2",
        "sample-1-0",
    ]
    assert runner.select_samples(suite, 0, "prefix") is suite


@pytest.mark.parametrize("chosen", [[], {"not-in-pinned-source"}])
def test_resume_selector_rejects_empty_or_unknown_ids(chosen):
    suite = sampling_suite([("a", "science", "test", 3)])
    with pytest.raises(ValueError, match="nonempty subset"):
        runner.select_ids(suite, chosen)


def test_resume_selector_deduplicates_requested_ids_and_preserves_alignment():
    suite = sampling_suite([("a", "science", "test", 3), ("b", "reading", "test", 2)])
    selected = runner.select_ids(suite, ["sample-1-1", "sample-0-2", "sample-1-1"])
    assert [row["sample_id"] for group in selected for row in group["rows"]] == [
        "sample-0-2",
        "sample-1-1",
    ]
    for group in selected:
        assert [sample.sample_id for sample in group["dataset"].samples] == [
            row["sample_id"] for row in group["rows"]
        ]


def planned_observation(sample_id, *, valid=True):
    return {
        "dataset": "sas-bench",
        "subject": "physics",
        "question_id": "q1",
        "sample_id": sample_id,
        "split": "holdout",
        "task_type": "numeric",
        "gold_score": 1 if valid else None,
        "original_label": 1 if valid else 11,
        "max_score": 10,
        "label_valid": valid,
        "criterion_count": 2,
        "question_answer_hash": f"hash-{sample_id}",
    }


def completed_observation(planned):
    return {
        **planned,
        "status": "succeeded",
        "predicted_score": "1",
        "selected_levels": {"band_0": "p1", "band_9": "p0"},
        "band_consistent": True,
        "calls": [],
        "agent_status": "completed",
        "grading_seconds": 1,
        "agent_seconds": 2,
    }


def test_early_stop_keeps_unattempted_rows_in_planned_denominators():
    planned = [planned_observation(f"answer-{index}") for index in range(4)]
    waves = [
        {
            "observations": [completed_observation(planned[0])],
            "audits": [],
            "schema_cleaned": True,
            "finished": False,
        }
    ]
    report = runner.summarize(waves, planned, SimpleNamespace(used=2, maximum=100))
    assert report["complete"] is False
    assert report["expected_samples"] == 4 and report["observed_samples"] == 1
    quality = report["quality"]["numeric"]
    assert quality["total"] == 4 and quality["predicted"] == 1
    assert quality["coverage"] == quality["end_to_end_exact_rate"] == 0.25
    assert quality["exact_agreement_on_predicted"] == 1
    assert quality["status_counts"] == {"not_attempted": 3, "succeeded": 1}
    assert report["engineering"]["end_to_end_scored_rate_all_planned"] == 0.25
    assert report["encoding"]["segmented_quality"]["numeric"]["total"] == 4


def test_invalid_source_labels_are_excluded_only_from_quality_and_counted_separately():
    planned = [planned_observation("valid")] + [
        planned_observation(f"invalid-{index}", valid=False) for index in range(16)
    ]
    waves = [
        {
            "observations": [completed_observation(row) for row in planned],
            "audits": [],
            "schema_cleaned": True,
            "finished": True,
        }
    ]
    report = runner.summarize(waves, planned, SimpleNamespace(used=34, maximum=100))
    assert report["complete"] is True
    assert report["expected_samples"] == report["observed_samples"] == 17
    assert report["invalid_source_labels"] == 16
    assert report["quality"]["numeric"]["total"] == 1
    assert report["deduplicated_nonconflicting_quality"]["numeric"]["total"] == 1
    assert report["engineering"]["end_to_end_scored"] == 17
    assert report["engineering"]["end_to_end_scored_rate_all_planned"] == 1


def test_empty_waves_preserve_valid_and_invalid_planned_counts_without_crashing():
    planned = [planned_observation("valid"), planned_observation("invalid", valid=False)]
    report = runner.summarize([], planned, SimpleNamespace(used=0, maximum=100))
    assert report["complete"] is False
    assert report["observed_samples"] == 0
    assert report["invalid_source_labels"] == 1
    assert report["quality"]["numeric"]["coverage"] == 0
    assert report["quality"]["numeric"]["normalized_mae_on_predicted"] is None
    assert report["engineering"]["status_counts"] == {"not_attempted": 2}
    assert report["engineering"]["end_to_end_scored_rate_all_planned"] == 0


def test_model_failure_receipt_excludes_freeform_provider_and_request_content():
    receipt = runner.model_failure_receipt(
        SimpleNamespace(
            kind="model_failed",
            sequence=4,
            body={
                "call_sequence": 2,
                "http_status": 402,
                "error_code": "provider_request_rejected",
                "message": "private provider payload",
                "authorization": "private credential",
                "request_body": {"answer": "private answer"},
                "provider_request_id": "opaque identifier",
                "usage": {"input_tokens": 10, "output_tokens": 0, "secret": "private value"},
            },
        )
    )
    assert receipt == {
        "kind": "model_failed",
        "event_sequence": 4,
        "call_sequence": 2,
        "http_status": 402,
        "error_code": "provider_request_rejected",
        "usage": {"input_tokens": 10, "output_tokens": 0},
    }
    assert "private" not in json.dumps(receipt)


def test_model_unknown_receipt_rejects_invalid_typed_diagnostic_fields():
    receipt = runner.model_failure_receipt(
        SimpleNamespace(
            kind="model_unknown",
            sequence=1,
            body={
                "error_code": "free form credential text",
                "http_status": "402",
                "call_sequence": True,
                "usage": {"input_tokens": -1, "output_tokens": True},
            },
        )
    )
    assert receipt == {
        "kind": "model_unknown",
        "event_sequence": 1,
        "error_code": "invalid_error_code",
        "usage": {},
    }


@pytest.mark.parametrize("status", [401, 402, 403])
@pytest.mark.parametrize("source", ["controller", "agent", "jev"])
def test_wave_blocker_detects_account_failures_at_each_layer(status, source):
    row = completed_observation(planned_observation("a"))
    wave = {"observations": [row]}
    if source == "controller":
        wave["controller_usage"] = {"failed_http_statuses": {str(status): 1}}
    elif source == "agent":
        row["agent_model_failures"] = [{"kind": "model_failed", "http_status": status}]
    else:
        row["calls"] = [{"kind": "evaluate", "status": "failed", "http_status": status}]
    assert runner.wave_blocker(wave) == f"provider_http_{status}"


@pytest.mark.parametrize(
    "code",
    [
        "provider_auth_error",
        "provider_insufficient_balance",
        "benchmark_request_ledger_unavailable",
        "benchmark_call_budget_exhausted",
    ],
)
def test_wave_blocker_detects_account_and_local_budget_codes_without_http(code):
    row = completed_observation(planned_observation("a"))
    row["agent_error_code"] = code
    assert runner.wave_blocker({"observations": [row]}) == code
    assert runner.wave_blocker({"controller_usage": {"error_codes": {code: 1}}}) == code


def test_all_failed_controllers_without_jev_calls_trip_fallback_breaker():
    wave = {
        "request_start": 20,
        "request_end": 20,
        "observations": [
            {"agent_status": "failed", "status": "not_created", "calls": []},
            {"agent_status": "failed", "status": "not_created", "calls": []},
        ],
    }
    assert runner.wave_blocker(wave) == "all_controllers_failed"
    wave["request_end"] = 21
    assert runner.wave_blocker(wave) is None
    wave["request_end"] = 20
    wave["observations"][0]["calls"] = [{"kind": "evaluate", "status": "unknown"}]
    assert runner.wave_blocker(wave) is None


@pytest.mark.parametrize("status,score", [("succeeded", "0"), ("abstained", None)])
def test_zero_scores_and_abstentions_do_not_trip_breaker(status, score):
    row = completed_observation(planned_observation("a"))
    row.update(status=status, predicted_score=score)
    wave = {"observations": [row], "expected_samples": 1}
    assert runner.wave_blocker(wave) is None
    assert runner.wave_completion(wave)["finished"] is True


def test_wave_visit_is_not_grading_completion_when_controllers_never_created_items():
    row = planned_observation("a")
    row.update(status="not_created", agent_status="failed", calls=[])
    outcome = runner.wave_completion({"observations": [row], "expected_samples": 1})
    assert outcome["visited_complete"] is True
    assert outcome["visited_samples"] == outcome["engine_failed_samples"] == 1
    assert outcome["terminal_grading_samples"] == 0
    assert outcome["grading_complete"] is False
    assert outcome["controllers_complete"] is False
    assert outcome["finished"] is False
    assert outcome["circuit_breaker"] == "all_controllers_failed"


def test_wave_reports_terminal_grading_failures_without_claiming_all_success():
    row = completed_observation(planned_observation("a"))
    row.update(status="failed", predicted_score=None)
    outcome = runner.wave_completion({"observations": [row], "expected_samples": 1})
    assert outcome["grading_complete"] is True
    assert outcome["finished"] is True
    assert outcome["circuit_breaker"] is None
    timed_out = runner.wave_completion(
        {
            "observations": [row],
            "expected_samples": 1,
            "execution": {"timed_out": True},
        }
    )
    assert timed_out["finished"] is False


def test_duplicate_observations_cannot_make_wave_complete():
    row = completed_observation(planned_observation("a"))
    outcome = runner.wave_completion({"observations": [row, row], "expected_samples": 2})
    assert outcome["visited_complete"] is False
    assert outcome["finished"] is False


@pytest.mark.postgres
async def test_predictions_export_controller_http402_once_per_event_not_per_answer(db, settings):
    config = type(settings).model_validate(
        {
            **settings.model_dump(),
            "jev_model": "jev-1.14.0",
            "jev_api_key": "protocol-fixture",
        }
    )
    await bootstrap(db, config)
    dataset = load_dataset(Path(__file__).parents[2] / "evals/samples/synthetic-v1.json")
    dataset = dataset.model_copy(update={"samples": dataset.samples[:2]})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(config, db)),
        base_url="http://fixture",
        headers={"Authorization": f"Bearer {config.dev_api_token}"},
    ) as client:
        targets, run_ids = await benchmark.seed(
            client,
            dataset,
            SimpleNamespace(runs=1, answers=2, controller="fake", deadline=30),
            None,
        )
    async with db.sessions.begin() as session:
        run = await session.get(AgentRun, run_ids[0])
        run.status, run.last_error = "failed", "provider_request_rejected"
        run.event_seq += 1
        session.add(
            AgentEvent(
                run_id=run.id,
                sequence=run.event_seq,
                epoch=run.epoch,
                kind="model_failed",
                body={
                    "call_sequence": 1,
                    "error_code": "provider_request_rejected",
                    "http_status": 402,
                    "message": "private provider payload",
                    "authorization": "private credential",
                },
            )
        )
    entries = [
        {
            "dataset": dataset,
            "targets": targets,
            "run_ids": run_ids,
            "rows": [planned_observation(sample.sample_id) for sample in dataset.samples],
        }
    ]
    rows, controller = await runner.predictions(db, entries)
    assert len(rows) == 2
    for row in rows:
        assert row["status"] == "not_created"
        assert row["agent_status"] == "failed"
        assert row["agent_error_code"] == "provider_request_rejected"
        assert row["agent_model_failures"][0]["http_status"] == 402
        assert "private" not in json.dumps(row["agent_model_failures"])
    assert controller["failed_http_statuses"] == {"402": 1}
    assert controller["error_codes"] == {"provider_request_rejected": 1}
    assert controller["model_failure_events"] == 1
    assert (
        runner.wave_blocker({"observations": rows, "controller_usage": controller})
        == "provider_http_402"
    )


@pytest.mark.postgres
async def test_audit_scopes_each_question_within_a_shared_schema(db, settings):
    config = type(settings).model_validate(
        {**settings.model_dump(), "jev_model": "jev-1.14.0", "jev_api_key": "protocol-fixture"}
    )
    await bootstrap(db, config)
    original = load_dataset(Path(__file__).parents[2] / "evals/samples/synthetic-v1.json")
    original = original.model_copy(update={"samples": original.samples[:2]})
    alternative = original.model_dump(mode="json")
    alternative.update(
        dataset_id="other-question",
        question="Another independent question with a different rubric.",
        question_version_id=str(uuid4()),
        rubric_version_id=str(uuid4()),
    )
    alternative["rubric"]["criteria"] = alternative["rubric"]["criteria"][:1]
    old_criterion = alternative["rubric"]["criteria"][0]["id"]
    alternative["rubric"]["criteria"][0]["id"] = "other-point"
    for sample in alternative["samples"]:
        sample["sample_id"] = "other-" + sample["sample_id"]
        sample["answer_version_id"] = str(uuid4())
        sample["answer_text"] = "Alternative response: " + sample["answer_text"]
        sample["gold_levels"] = {"other-point": sample["gold_levels"][old_criterion]}
    alternative = Dataset.model_validate(alternative)
    originals = benchmark.ProtocolStub(original, 0.001, False)
    alternatives = benchmark.ProtocolStub(alternative, 0.001, False)

    async def provider(request):
        answer = json.loads(request.content)["state"]["student_answer"]
        selected = originals if answer in originals.answers else alternatives
        return await selected.respond(request)

    args = SimpleNamespace(runs=1, answers=2, controller="fake", deadline=30)
    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(config, db)),
            base_url="http://fixture",
            headers={"Authorization": f"Bearer {config.dev_api_token}"},
        ) as client,
        httpx.AsyncClient(transport=httpx.MockTransport(provider)) as provider_client,
    ):
        left_targets, left_runs = await benchmark.seed(client, original, args, originals)
        right_targets, right_runs = await benchmark.seed(client, alternative, args, alternatives)
        execution = await benchmark.execute(
            db,
            config,
            SimpleNamespace(workers=2, deadline=30),
            benchmark.ProviderBudget(20),
            provider_client,
        )
    assert not execution["timed_out"]
    assert not execution["worker_errors"]
    audit_args = SimpleNamespace(mode="stub", runs=1, slo_seconds=30)
    left = await benchmark.audit(db, original, left_targets, audit_args, run_ids=left_runs)
    right = await benchmark.audit(db, alternative, right_targets, audit_args, run_ids=right_runs)
    for report in (left, right):
        assert report["invariants_passed"], report["violations"]
        assert report["submitted_answers"] == report["scored_answers"] == 2
        assert report["completed_agents"] == 1
        assert report["end_to_end_success_rate"] == 1
    assert left["assessments"] == 2 * len(original.rubric.criteria)
    assert right["assessments"] == 2 * len(alternative.rubric.criteria)
    assert left["assessments"] != right["assessments"]

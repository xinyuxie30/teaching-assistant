"""Quality rates must expose missing answers and preserve benchmark score semantics."""

import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "public_grading_metrics", Path(__file__).parents[1] / "tools/public_grading_metrics.py"
)
metrics = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(metrics)


def observation(sample_id, *, gold=0, predicted=0, maximum=10, status="succeeded", **extra):
    return {
        "dataset": "human-labels",
        "subject": "physics",
        "question_id": "q1",
        "sample_id": sample_id,
        "task_type": "numeric",
        "split": "holdout",
        "gold_score": gold,
        "max_score": maximum,
        "predicted_score": predicted,
        "status": status,
        **extra,
    }


def test_failed_and_abstained_answers_are_not_scored_as_zero():
    result = metrics.summarize_public_grading(
        [
            observation("zero"),
            observation("failure", predicted=None, status="failed"),
            observation("abstention", predicted=None, status="succeeded"),
        ]
    )["numeric"]
    assert result["total"] == 3
    assert result["predicted"] == result["exact_matches"] == 1
    assert result["coverage"] == result["end_to_end_exact_rate"] == 1 / 3
    assert result["exact_agreement_on_predicted"] == 1
    assert result["normalized_mae_on_predicted"] == 0


def test_normalization_uses_each_questions_maximum_and_signed_bias():
    result = metrics.summarize_public_grading(
        [
            observation("a", gold="2.00", predicted="2.50", maximum=5),
            observation("b", gold=6, predicted=4, maximum=10, question_id="q2"),
        ]
    )
    assert result["numeric"]["normalized_mae_on_predicted"] == pytest.approx(0.15)
    assert result["numeric"]["normalized_bias_on_predicted"] == pytest.approx(-0.05)
    assert result["numeric"]["severe_error_count"] == 1  # Inclusive 20% boundary.
    assert len(result["by_question"]) == 2
    assert json.loads(json.dumps(result))["observations"] == 2


def test_binary_classification_excludes_missing_from_f1_but_not_completion():
    rows = [
        observation(str(i), gold=gold, predicted=predicted, maximum=1, task_type="binary")
        for i, (gold, predicted) in enumerate([(0, 0), (0, 1), (1, 1), (1, None)])
    ]
    result = metrics.summarize_public_grading(rows)
    assert result["numeric"]["total"] == 0
    binary = result["binary"]
    assert binary["confusion_matrix"] == [[1, 1], [0, 1]]
    assert binary["accuracy_on_predicted"] == pytest.approx(2 / 3)
    assert binary["macro_f1_on_predicted"] == pytest.approx(2 / 3)
    assert binary["end_to_end_correct_rate"] == 0.5
    assert binary["per_class"][1]["missing_predictions"] == 1


def test_binary_absent_class_does_not_silently_change_macro_average():
    result = metrics.summarize_public_grading(
        [observation("a", gold=1, predicted=1, maximum=1, task_type="binary")]
    )["binary"]
    assert result["accuracy_on_predicted"] == 1
    assert result["macro_f1_on_predicted"] == 0.5
    assert result["per_class"][0]["precision"] is None
    assert result["per_class"][0]["f1"] == 0


def test_no_predictions_produces_unknown_quality_instead_of_perfect_agreement():
    result = metrics.summarize_public_grading([observation("a", predicted=None, status="pending")])[
        "numeric"
    ]
    assert result["coverage"] == result["end_to_end_exact_rate"] == 0
    assert result["exact_agreement_on_predicted"] is None
    assert result["normalized_mae_on_predicted"] is None
    empty = metrics.summarize_public_grading([])
    assert empty["numeric"]["coverage"] is None
    assert empty["binary"]["macro_f1_on_predicted"] is None


def test_duplicate_response_cannot_inflate_quality_even_across_splits():
    with pytest.raises(ValueError, match="Duplicate quality"):
        metrics.summarize_public_grading([observation("a"), observation("a", split="calibration")])


def test_same_local_id_from_different_questions_is_not_a_duplicate():
    result = metrics.summarize_public_grading(
        [observation("a"), observation("a", question_id="q2", subject="chemistry")]
    )
    assert result["observations"] == 2
    assert len(result["by_subject"]) == 2


@pytest.mark.parametrize(
    "extra",
    [
        {"predicted_score": 2, "status": "failed"},
        {"predicted_score": "NaN"},
        {"predicted_score": "Infinity"},
        {"predicted_score": -1},
        {"predicted_score": 11},
        {"gold_score": 11},
        {"gold_score": True},
        {"max_score": 0},
        {"task_type": "binary", "gold_score": 2, "max_score": 1},
        {"task_type": "binary", "predicted_score": 0.5, "max_score": 1},
        {"task_type": "ordinal-category"},
        {"sample_id": ""},
    ],
)
def test_invalid_or_stale_scores_are_rejected(extra):
    row = observation("a")
    row.update(extra)
    with pytest.raises(ValueError):
        metrics.summarize_public_grading([row])


def test_subject_and_split_reports_remain_separate():
    result = metrics.summarize_public_grading(
        [
            observation("a"),
            observation("b", split="unseen_questions", predicted=2),
            observation("c", dataset="other", subject="biology", predicted=1),
        ]
    )
    assert len(result["by_dataset"]) == 2
    assert len(result["by_split"]) == 3
    assert len(result["by_subject"]) == 3
    original = next(group for group in result["by_split"] if group["split"] == "holdout")
    assert original["metrics"]["numeric"]["total"] == 1


@pytest.mark.parametrize("cutoff", [0, -0.1, 1.1, float("nan")])
def test_invalid_severe_error_threshold_is_rejected(cutoff):
    with pytest.raises(ValueError):
        metrics.summarize_public_grading([], severe_error_fraction=cutoff)

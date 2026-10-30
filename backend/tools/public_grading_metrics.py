"""Offline quality metrics for public, human-labelled grading benchmarks.

Gold labels belong only in these offline observations, never in provider requests.
Numeric scores and binary answer correctness are distinct tasks. Failures remain
in coverage and end-to-end denominators but are never silently converted to zero.
"""

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

SUCCESS_STATUSES = frozenset({"succeeded", "assessed", "scored"})
TASK_TYPES = frozenset({"numeric", "binary"})


def _number(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} must be a finite number")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{field} must be a finite number") from error
    if not number.is_finite():
        raise ValueError(f"{field} must be a finite number")
    return number


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    return value.strip()


def _validated(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    observations, seen = [], set()
    for row in rows:
        item = {
            field: _text(row.get(field), field)
            for field in ("dataset", "subject", "question_id", "sample_id", "task_type", "status")
        }
        item["split"] = _text(row.get("split", "unspecified"), "split")
        identity = (item["dataset"], item["question_id"], item["sample_id"])
        if identity in seen:
            raise ValueError(f"Duplicate quality observation: {identity}")
        seen.add(identity)
        if item["task_type"] not in TASK_TYPES:
            raise ValueError("task_type must be numeric or binary")
        maximum = _number(row.get("max_score"), "max_score")
        gold = _number(row.get("gold_score"), "gold_score")
        if maximum <= 0 or not 0 <= gold <= maximum:
            raise ValueError("max_score must be positive and gold_score within its range")
        predicted = (
            None
            if row.get("predicted_score") is None
            else _number(row["predicted_score"], "predicted_score")
        )
        if predicted is not None:
            if item["status"] not in SUCCESS_STATUSES:
                raise ValueError("A failed or incomplete observation cannot carry a prediction")
            if not 0 <= predicted <= maximum:
                raise ValueError("predicted_score must be within the score range")
        if item["task_type"] == "binary" and (
            maximum != 1 or gold not in {0, 1} or predicted not in {None, 0, 1}
        ):
            raise ValueError("Binary correctness requires max_score=1 and labels 0 or 1")
        item.update(gold_score=gold, max_score=maximum, predicted_score=predicted)
        observations.append(item)
    return observations


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _mean(values: list[Decimal]) -> float | None:
    return float(sum(values, Decimal(0)) / len(values)) if values else None


def _base(rows: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    predicted = [row for row in rows if row["predicted_score"] is not None]
    return {
        "total": len(rows),
        "predicted": len(predicted),
        "missing_prediction": len(rows) - len(predicted),
        "coverage": _ratio(len(predicted), len(rows)),
        "status_counts": dict(sorted(Counter(row["status"] for row in rows).items())),
    }, predicted


def _numeric(rows: list[dict[str, Any]], cutoff: Decimal) -> dict[str, Any]:
    result, predicted = _base(rows)
    errors = [(row["predicted_score"] - row["gold_score"]) / row["max_score"] for row in predicted]
    exact = sum(error == 0 for error in errors)
    severe = sum(abs(error) >= cutoff for error in errors)
    result.update(
        exact_matches=exact,
        exact_agreement_on_predicted=_ratio(exact, len(predicted)),
        end_to_end_exact_rate=_ratio(exact, len(rows)),
        normalized_mae_on_predicted=_mean([abs(error) for error in errors]),
        normalized_bias_on_predicted=_mean(errors),
        severe_error_fraction=float(cutoff),
        severe_error_count=severe,
        severe_error_rate_on_predicted=_ratio(severe, len(predicted)),
    )
    return result


def _binary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    result, predicted = _base(rows)
    matrix = [[0, 0], [0, 0]]
    missing = [0, 0]
    for row in rows:
        actual = int(row["gold_score"])
        if row["predicted_score"] is None:
            missing[actual] += 1
        else:
            matrix[actual][int(row["predicted_score"])] += 1
    correct = matrix[0][0] + matrix[1][1]
    per_class = []
    for label in range(2):
        true_positive = matrix[label][label]
        false_positive = matrix[1 - label][label]
        false_negative = matrix[label][1 - label]
        denominator = 2 * true_positive + false_positive + false_negative
        per_class.append(
            {
                "label": label,
                "meaning": "correct" if label else "not_correct",
                "support_all": sum(matrix[label]) + missing[label],
                "support_predicted": sum(matrix[label]),
                "missing_predictions": missing[label],
                "precision": _ratio(true_positive, true_positive + false_positive),
                "recall": _ratio(true_positive, true_positive + false_negative),
                "f1": 2 * true_positive / denominator if denominator else 0.0,
            }
        )
    result.update(
        correct_predictions=correct,
        accuracy_on_predicted=_ratio(correct, len(predicted)),
        end_to_end_correct_rate=_ratio(correct, len(rows)),
        macro_f1_on_predicted=(sum(item["f1"] for item in per_class) / 2 if predicted else None),
        confusion_matrix=matrix,
        confusion_matrix_axes="Rows: gold [not_correct, correct]; columns: predicted in same order",
        per_class=per_class,
        macro_f1_convention="Fixed labels [0, 1]; a class with zero F1 denominator contributes 0",
    )
    return result


def _aggregate(rows: list[dict[str, Any]], cutoff: Decimal) -> dict[str, Any]:
    return {
        "observations": len(rows),
        "numeric": _numeric([row for row in rows if row["task_type"] == "numeric"], cutoff),
        "binary": _binary([row for row in rows if row["task_type"] == "binary"]),
    }


def _grouped(
    rows: list[dict[str, Any]], fields: tuple[str, ...], cutoff: Decimal
) -> list[dict[str, Any]]:
    groups = defaultdict(list)
    for row in rows:
        groups[tuple(row[field] for field in fields)].append(row)
    return [
        {**dict(zip(fields, key, strict=True)), "metrics": _aggregate(groups[key], cutoff)}
        for key in sorted(groups)
    ]


def summarize_public_grading(
    rows: Iterable[Mapping[str, Any]], *, severe_error_fraction: float = 0.2
) -> dict[str, Any]:
    """Summarize independent predictions, rejecting duplicates and invalid scores.

    Required fields are dataset, subject, question_id, sample_id, task_type
    (numeric/binary), gold_score, max_score, predicted_score (nullable), and
    status. Optional split defaults to unspecified and is preserved in groups.
    Identity excludes split: copying the same response to another split cannot
    inflate the quality sample size. Repeated load runs must be summarized apart.
    """
    cutoff = _number(severe_error_fraction, "severe_error_fraction")
    if not 0 < cutoff <= 1:
        raise ValueError("severe_error_fraction must be in (0, 1]")
    observations = _validated(rows)
    return {
        "schema_version": "public-grading-quality-v1",
        "averaging": "One unique response has one vote within its task type",
        "missing_prediction_policy": (
            "Missing predictions remain in coverage and end-to-end denominators; "
            "score errors and classification F1 use predictions only"
        ),
        **_aggregate(observations, cutoff),
        "by_dataset": _grouped(observations, ("dataset",), cutoff),
        "by_split": _grouped(observations, ("dataset", "split"), cutoff),
        "by_subject": _grouped(observations, ("dataset", "split", "subject"), cutoff),
        "by_question": _grouped(
            observations, ("dataset", "split", "subject", "question_id"), cutoff
        ),
    }

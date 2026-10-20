import copy
from decimal import Decimal

import pytest
from pydantic import ValidationError
from teaching_assistant.demo import DEMO_RUBRIC
from teaching_assistant.domain import (
    DomainError,
    Evaluation,
    Evidence,
    EvidenceOutput,
    Rubric,
    calculate_score,
    candidate,
    validate_evidence,
)


def evaluation(**kwargs):
    return Evaluation(
        requested_model="test", returned_model="test", adapter_version="test", **kwargs
    )


def test_provider_continuous_score_is_not_a_student_grade():
    rubric = Rubric.model_validate(DEMO_RUBRIC)
    result = evaluation(
        probabilities={"missing": 0.05, "partial": 0.1, "complete": 0.85},
        provider_score=0.9,
        provider_confidence=0.95,
    )
    level = candidate(result, rubric.criteria[0])
    assert level == "complete"
    assert calculate_score(rubric, {"fixed_rule": level, "traceability": "missing"}) == Decimal(
        "6.00"
    )


def test_ties_abstain_and_missing_assessments_are_not_zero():
    rubric = Rubric.model_validate(DEMO_RUBRIC)
    assert (
        candidate(evaluation(probabilities={"missing": 0.5, "complete": 0.5}), rubric.criteria[1])
        is None
    )
    assert calculate_score(rubric, {"fixed_rule": "complete", "traceability": None}) is None
    with pytest.raises(DomainError, match="Every rubric criterion"):
        calculate_score(rubric, {"fixed_rule": "complete"})


@pytest.mark.parametrize(
    "probabilities",
    [
        {"missing": 0.8, "complete": 0.8},
        {"missing": -0.1, "complete": 1.1},
        {"missing": float("nan"), "complete": 1.0},
        {"missing": 1.0, "unexpected": 0.0},
    ],
)
def test_invalid_provider_distribution_is_rejected(probabilities):
    rubric = Rubric.model_validate(DEMO_RUBRIC)
    with pytest.raises(DomainError):
        candidate(evaluation(probabilities=probabilities), rubric.criteria[1])


def test_exact_evidence_handles_unicode_and_line_endings():
    answer = " 😀\r\n证据😀 "
    citation = Evidence(quote="证据😀", start=4, end=7)
    valid = EvidenceOutput(citations=[citation], extractor_version="test")
    validate_evidence(answer, valid)
    with pytest.raises(DomainError):
        validate_evidence(answer.replace("\r\n", "\n"), valid)


def test_rubric_requires_distinct_levels_and_fixed_points():
    payload = copy.deepcopy(DEMO_RUBRIC)
    payload["criteria"][0]["levels"][1]["id"] = "missing"
    with pytest.raises(ValidationError):
        Rubric.model_validate(payload)
    payload = copy.deepcopy(DEMO_RUBRIC)
    payload["criteria"][0]["levels"][2]["points"] = "5.00"
    with pytest.raises(ValidationError):
        Rubric.model_validate(payload)

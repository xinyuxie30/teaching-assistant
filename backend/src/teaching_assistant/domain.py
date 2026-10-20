"""Business contracts. No web, database or model-provider dependencies."""

import hashlib
import json
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Nonempty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=10000)]
Key = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,80}$")]
Points = Annotated[Decimal, Field(ge=0, le=10000, max_digits=8, decimal_places=2)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DomainError(Exception):
    def __init__(self, code: str, detail: str, status: int = 422):
        self.code, self.detail, self.status = code, detail, status
        super().__init__(detail)


class ModelError(DomainError):
    """Sanitized provider failure, including metadata needed for retry and cost accounting."""

    def __init__(
        self,
        code: str,
        *,
        retryable: bool,
        retry_after_seconds: int | None = None,
        provider_request_id: str | None = None,
        http_status: int | None = None,
        returned_model: str | None = None,
        usage: dict[str, int] | None = None,
    ):
        super().__init__(code, "Model operation failed; inspect the recorded error code")
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
        self.provider_request_id = provider_request_id
        self.http_status = http_status
        self.returned_model = returned_model
        self.usage = usage


class Level(Contract):
    id: Key
    ordinal: int = Field(ge=0, le=9)
    description: Nonempty
    points: Points


class Criterion(Contract):
    id: Key
    semantic_version: int = Field(default=1, ge=1)
    description: Nonempty
    maximum_points: Points
    levels: list[Level] = Field(min_length=2, max_length=10)

    @model_validator(mode="after")
    def validate_levels(self):
        if len({x.id for x in self.levels}) != len(self.levels):
            raise ValueError("Duplicate level id")
        if sorted(x.ordinal for x in self.levels) != list(range(len(self.levels))):
            raise ValueError("Level ordinals must be consecutive starting at 0")
        ordered = sorted(self.levels, key=lambda x: x.ordinal)
        values = [x.points for x in ordered]
        if values != sorted(values) or max(values) != self.maximum_points:
            raise ValueError("Level points must be monotonic and reach the criterion maximum")
        return self


class Rubric(Contract):
    global_instructions: Nonempty
    criteria: list[Criterion] = Field(min_length=1, max_length=30)
    selection_policy: Literal["unique-argmax-v1"] = "unique-argmax-v1"
    rounding_policy: Literal["total-half-up-2dp-v1"] = "total-half-up-2dp-v1"

    @model_validator(mode="after")
    def unique_criteria(self):
        if len({c.id for c in self.criteria}) != len(self.criteria):
            raise ValueError("Duplicate criterion id")
        return self

    @property
    def maximum_points(self) -> Decimal:
        return sum((c.maximum_points for c in self.criteria), Decimal(0))


class Evidence(Contract):
    quote: str = Field(min_length=1, max_length=100000)
    start: int = Field(ge=0)
    end: int = Field(gt=0)


class AssessmentInput(Contract):
    question_version_id: UUID
    answer_version_id: UUID
    rubric_version_id: UUID
    question: str
    reference: str
    answer: str
    global_instructions: str
    criterion: Criterion
    profile: str


class Evaluation(Contract):
    candidate_level_id: str | None = None
    probabilities: dict[str, float] | None = None
    provider_score: float | None = Field(default=None, allow_inf_nan=False)
    provider_confidence: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    requested_model: str
    returned_model: str
    adapter_version: str
    usage: dict[str, int] | None = None
    provider_request_id: str | None = None
    prompt_hash: str | None = None
    http_status: int | None = None


class EvidenceOutput(Contract):
    citations: list[Evidence]
    absence_claim: bool = False
    extractor_version: str


def digest(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def text_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def candidate(evaluation: Evaluation, criterion: Criterion) -> str | None:
    allowed = {x.id for x in criterion.levels}
    if evaluation.probabilities is not None:
        values = evaluation.probabilities
        if set(values) != allowed:
            raise DomainError("invalid_distribution", "Distribution must cover exactly all levels")
        # Decimal parsing rejects NaN/Infinity without relying on float comparisons.
        weights = {key: Decimal(str(value)) for key, value in values.items()}
        if any(not x.is_finite() or x < 0 or x > 1 for x in weights.values()):
            raise DomainError("invalid_distribution", "Probabilities must be finite and in [0, 1]")
        if abs(sum(weights.values()) - Decimal(1)) > Decimal("0.000001"):
            raise DomainError("invalid_distribution", "Probabilities must sum to 1")
        maximum = max(weights.values())
        winners = [key for key, value in weights.items() if value == maximum]
        selected = winners[0] if len(winners) == 1 else None
        if evaluation.candidate_level_id not in (None, selected):
            raise DomainError(
                "invalid_candidate", "Candidate contradicts the probability distribution"
            )
        return selected
    if evaluation.candidate_level_id is not None and evaluation.candidate_level_id not in allowed:
        raise DomainError("invalid_candidate", "Unknown candidate level")
    return evaluation.candidate_level_id


def validate_evidence(answer: str, evidence: EvidenceOutput) -> None:
    for citation in evidence.citations:
        if citation.end > len(answer) or answer[citation.start : citation.end] != citation.quote:
            raise DomainError(
                "invalid_evidence", "Citation is not an exact code-point slice of answer"
            )
    if evidence.absence_claim and evidence.citations:
        raise DomainError(
            "invalid_evidence", "An absence claim cannot include supporting citations"
        )


def calculate_score(rubric: Rubric, selected: dict[str, str | None]) -> Decimal | None:
    if set(selected) != {c.id for c in rubric.criteria}:
        raise DomainError("incomplete_assessment", "Every rubric criterion must occur exactly once")
    if any(level is None for level in selected.values()):
        return None
    total = Decimal(0)
    for criterion in rubric.criteria:
        levels = {level.id: level.points for level in criterion.levels}
        if selected[criterion.id] not in levels:
            raise DomainError("invalid_candidate", "Unknown selected level")
        total += levels[selected[criterion.id]]
    return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

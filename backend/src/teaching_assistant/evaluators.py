"""Replaceable provider boundary; fake-v1 is deliberately not semantic grading."""

from typing import Protocol

from teaching_assistant.domain import (
    AssessmentInput,
    Evaluation,
    Evidence,
    EvidenceOutput,
    ModelError,
)


class Evaluator(Protocol):
    async def assess(self, request: AssessmentInput) -> Evaluation: ...


class EvidenceExtractor(Protocol):
    async def extract(self, request: AssessmentInput) -> EvidenceOutput: ...


class FakeEvaluator:
    requested_model = "fake-v1"
    adapter_version = "fake-v1"

    async def assess(self, request: AssessmentInput) -> Evaluation:
        # Always choose the lowest level. Never disguise keyword matching as model quality.
        selected = min(request.criterion.levels, key=lambda level: level.ordinal).id
        return Evaluation(
            candidate_level_id=selected,
            probabilities=None,
            requested_model="fake-v1",
            returned_model="fake-v1",
            adapter_version="fake-v1",
            usage={},
        )


class FakeEvidenceExtractor:
    requested_model = "fake-prefix-v1"
    adapter_version = "fake-prefix-v1"

    async def extract(self, request: AssessmentInput) -> EvidenceOutput:
        # Independent of the candidate level; existence is checked, support is left to the teacher.
        quote = request.answer[:240]
        return EvidenceOutput(
            citations=[Evidence(quote=quote, start=0, end=len(quote))] if quote else [],
            absence_claim=not quote,
            extractor_version="fake-prefix-v1",
        )


class ManualEvidenceExtractor:
    """Local placeholder for profiles that leave evidence selection to the teacher."""

    requested_model = "manual-evidence-v1"
    adapter_version = "manual-evidence-v1"

    async def extract(self, request: AssessmentInput) -> EvidenceOutput:
        return EvidenceOutput(
            citations=[], absence_claim=False, extractor_version="manual-evidence-v1"
        )


def build_adapters(settings, profile) -> tuple[Evaluator, EvidenceExtractor]:
    if profile.id == "fake-v1":
        return FakeEvaluator(), FakeEvidenceExtractor()
    if profile.manifest["provider"] == "deepseek":
        from teaching_assistant.deepseek import DeepSeekEvaluator, DeepSeekEvidenceExtractor
        from teaching_assistant.grading_context import EVIDENCE_VERSION

        extractor = (
            DeepSeekEvidenceExtractor(settings, profile)
            if profile.manifest["evidence_strategy"] == EVIDENCE_VERSION
            else ManualEvidenceExtractor()
        )
        return DeepSeekEvaluator(settings, profile), extractor
    if profile.manifest["provider"] != "typesafe":
        raise ModelError("unsupported_model_provider", retryable=False)
    from teaching_assistant.jev import JevEvaluator

    return JevEvaluator(settings, profile), ManualEvidenceExtractor()

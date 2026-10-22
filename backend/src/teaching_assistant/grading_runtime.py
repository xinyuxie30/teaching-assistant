"""An explicit grading plan with a closed step registry and preflight checks.

The Worker owns transactions, leases and checkpoints. A model can return data only;
it cannot change this plan, confirm marks or invoke arbitrary business tools.
"""

from teaching_assistant.domain import (
    AssessmentInput,
    Evaluation,
    EvidenceOutput,
    ModelError,
    candidate,
    validate_evidence,
)

GRADING_STEPS = ("evaluate", "extract_evidence")


class GradingRuntime:
    def __init__(self, evaluator, extractor):
        self.adapters = {"evaluate": evaluator, "extract_evidence": extractor}

    def adapter(self, kind: str):
        if kind not in GRADING_STEPS:
            raise ModelError("unsupported_grading_step", retryable=False)
        return self.adapters[kind]

    def preflight(self, request: AssessmentInput, kind: str) -> None:
        prepare = getattr(self.adapter(kind), "prepare", None)
        if callable(prepare):
            prepare(request)

    async def invoke(self, request: AssessmentInput, kind: str):
        adapter = self.adapter(kind)
        if kind == "evaluate":
            output = Evaluation.model_validate(await adapter.assess(request))
            candidate(output, request.criterion)
        else:
            output = EvidenceOutput.model_validate(await adapter.extract(request))
            validate_evidence(request.answer, output)
        return output

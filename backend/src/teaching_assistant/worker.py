"""A small durable workflow: PostgreSQL leases, fenced checkpoints and bounded calls."""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from teaching_assistant.db import (
    AnswerVersion,
    Assessment,
    Attempt,
    Database,
    EvaluationProfile,
    GradingItem,
    ModelCall,
    QuestionVersion,
    RubricVersion,
    Step,
)
from teaching_assistant.domain import (
    AssessmentInput,
    DomainError,
    Evaluation,
    EvidenceOutput,
    ModelError,
    Rubric,
    calculate_score,
    candidate,
    digest,
    text_digest,
    validate_evidence,
)
from teaching_assistant.evaluators import (
    Evaluator,
    EvidenceExtractor,
    build_adapters,
)
from teaching_assistant.grading_runtime import GRADING_STEPS, GradingRuntime
from teaching_assistant.profiles import ProfileRegistry
from teaching_assistant.service import refresh_batches
from teaching_assistant.settings import Settings
from teaching_assistant.worker_backoff import failure_delay

logger = logging.getLogger("teaching_assistant.worker")


class LostLease(Exception):
    pass


class BudgetExhausted(Exception):
    pass


@dataclass(frozen=True)
class Lease:
    item_id: UUID
    epoch: int
    owner: str


class Worker:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        evaluator: Evaluator | None = None,
        extractor: EvidenceExtractor | None = None,
        *,
        worker_id: str | None = None,
        item_ids: tuple[UUID, ...] | None = None,
    ):
        self.db, self.settings = db, settings
        self.evaluator = evaluator
        self.extractor = extractor
        self.profiles = ProfileRegistry(settings)
        self._adapters: dict[str, tuple[Evaluator, EvidenceExtractor]] = {}
        self.worker_id = worker_id or f"worker-{uuid4()}"
        self.item_ids = item_ids

    def adapters(self, profile_id: str) -> tuple[Evaluator, EvidenceExtractor]:
        if profile_id not in self._adapters:
            profile = self.profiles.resolve(profile_id)
            if profile.id != "fake-v1" and (
                self.evaluator is not None or self.extractor is not None
            ):
                raise ModelError("real_profile_adapter_override_rejected", retryable=False)
            evaluator, extractor = build_adapters(self.settings, profile)
            self._adapters[profile_id] = (self.evaluator or evaluator, self.extractor or extractor)
        return self._adapters[profile_id]

    def log(self, event: str, lease: Lease, **details):
        logger.info(
            json.dumps(
                {
                    "event": event,
                    "item_id": str(lease.item_id),
                    "epoch": lease.epoch,
                    "worker_id": lease.owner,
                    **details,
                }
            )
        )

    async def claim(self) -> Lease | None:
        while True:
            async with self.db.sessions.begin() as session:
                now = await session.scalar(select(func.clock_timestamp()))
                query = select(GradingItem).where(
                    GradingItem.evaluation_profile.in_(self.profiles.supported_ids()),
                    or_(
                        and_(
                            GradingItem.status.in_(["pending", "retry_wait"]),
                            GradingItem.ready_at <= now,
                        ),
                        and_(GradingItem.status == "running", GradingItem.lease_until <= now),
                    ),
                )
                if self.item_ids is not None:
                    query = query.where(GradingItem.id.in_(self.item_ids))
                item = await session.scalar(
                    query.order_by(GradingItem.ready_at, GradingItem.id)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
                if item is None:
                    return None
                # A timed-out process may have reached its provider. Never pretend it made no call.
                await session.execute(
                    update(Attempt)
                    .where(Attempt.item_id == item.id, Attempt.status == "running")
                    .values(status="abandoned", finished_at=now)
                )
                await session.execute(
                    update(ModelCall)
                    .where(ModelCall.item_id == item.id, ModelCall.status == "dispatched")
                    .values(status="unknown", error_code="lease_expired")
                )
                if item.attempts >= item.max_attempts:
                    item.status, item.last_error = "failed", "attempt_budget_exhausted"
                    item.lease_until, item.lease_owner = None, None
                    await session.flush()
                    await refresh_batches(session, item.id)
                    continue
                item.epoch += 1
                item.attempts += 1
                item.status, item.last_error = "running", None
                item.lease_owner = self.worker_id
                item.lease_until = now + timedelta(seconds=self.settings.lease_seconds)
                session.add(
                    Attempt(
                        item_id=item.id,
                        epoch=item.epoch,
                        worker_id=self.worker_id,
                        status="running",
                    )
                )
                await session.flush()
                await refresh_batches(session, item.id)
                # Batch aggregation can wait behind another transaction. Start the
                # execution lease only after that wait, while still owning the item.
                now = await session.scalar(select(func.clock_timestamp()))
                item.lease_until = now + timedelta(seconds=self.settings.lease_seconds)
                lease = Lease(item.id, item.epoch, self.worker_id)
            self.log("claimed", lease)
            return lease

    async def fenced_item(self, session: AsyncSession, lease: Lease) -> GradingItem:
        item = await session.scalar(
            select(GradingItem).where(GradingItem.id == lease.item_id).with_for_update()
        )
        # Check database time after acquiring the lock; pre-lock timestamps can admit stale writes.
        now = await session.scalar(select(func.clock_timestamp()))
        if (
            item is None
            or item.status != "running"
            or item.epoch != lease.epoch
            or item.lease_owner != lease.owner
            or item.lease_until is None
            or item.lease_until <= now
        ):
            raise LostLease()
        item.lease_until = now + timedelta(seconds=self.settings.lease_seconds)
        return item

    async def context(self, lease: Lease) -> tuple[Rubric, list[AssessmentInput]]:
        async with self.db.sessions() as session:
            item = await session.get(GradingItem, lease.item_id)
            profile = self.profiles.resolve(item.evaluation_profile)
            stored = await session.get(EvaluationProfile, profile.id)
            if (
                stored is None
                or stored.fingerprint != profile.fingerprint
                or stored.manifest != profile.manifest
            ):
                raise ModelError("evaluation_profile_mismatch", retryable=False)
            answer = await session.get(AnswerVersion, item.answer_version_id)
            frozen = await session.get(RubricVersion, item.rubric_version_id)
            question = await session.get(QuestionVersion, frozen.question_version_id)
            rubric = Rubric.model_validate(frozen.body)
            return rubric, [
                AssessmentInput(
                    question_version_id=question.id,
                    answer_version_id=answer.id,
                    rubric_version_id=frozen.id,
                    question=question.prompt,
                    reference=question.reference,
                    answer=answer.answer_text,
                    global_instructions=rubric.global_instructions,
                    criterion=criterion,
                    profile=item.evaluation_profile,
                )
                for criterion in rubric.criteria
            ]

    async def reserve_call(
        self, lease: Lease, request: AssessmentInput, kind: str, input_hash: str
    ) -> UUID:
        evaluator, extractor = self.adapters(request.profile)
        runtime = GradingRuntime(evaluator, extractor)
        runtime.preflight(request, kind)
        adapter = runtime.adapter(kind)
        prompt_hash = getattr(adapter, "prompt_hash", None)
        prompt_hash = prompt_hash(request) if callable(prompt_hash) else None
        async with self.db.sessions.begin() as session:
            item = await self.fenced_item(session, lease)
            if item.calls_used >= item.max_calls:
                raise BudgetExhausted()
            item.calls_used += 1
            call = ModelCall(
                item_id=item.id,
                epoch=lease.epoch,
                criterion_id=request.criterion.id,
                kind=kind,
                input_hash=input_hash,
                status="dispatched",
                requested_model=getattr(adapter, "requested_model", request.profile),
                adapter_version=getattr(adapter, "adapter_version", None),
                prompt_hash=prompt_hash,
            )
            session.add(call)
            await session.flush()
            return call.id

    async def execute_step(self, lease: Lease, request: AssessmentInput, kind: str) -> Step:
        evaluator, extractor = self.adapters(request.profile)
        runtime = GradingRuntime(evaluator, extractor)
        runtime.adapter(kind)
        input_hash = digest(
            {"contract": "assessment-v1", "kind": kind, **request.model_dump(mode="json")}
        )
        async with self.db.sessions() as session:
            saved = await session.scalar(
                select(Step).where(
                    Step.item_id == lease.item_id,
                    Step.criterion_id == request.criterion.id,
                    Step.kind == kind,
                )
            )
            if saved is not None:
                if saved.input_hash != input_hash:
                    raise DomainError("checkpoint_mismatch", "Frozen checkpoint input changed")
                return saved
        call_id = await self.reserve_call(lease, request, kind, input_hash)
        started = time.monotonic()
        try:
            async with asyncio.timeout(self.settings.call_timeout_seconds):
                output = await runtime.invoke(request, kind)
        except asyncio.CancelledError:
            # Leave the persisted call as unknown until the lease is reclaimed.
            raise
        except Exception as exc:
            code = (
                "model_timeout"
                if isinstance(exc, TimeoutError)
                else (exc.code if isinstance(exc, DomainError) else "provider_or_contract_error")
            )
            async with self.db.sessions.begin() as session:
                await self.fenced_item(session, lease)
                call = await session.get(ModelCall, call_id)
                call.status = (
                    "unknown"
                    if code in {"model_timeout", "provider_timeout", "provider_transport_error"}
                    else "failed"
                )
                call.error_code = code
                call.duration_ms = int((time.monotonic() - started) * 1000)
                call.error_retryable = exc.retryable if isinstance(exc, ModelError) else True
                if isinstance(exc, ModelError):
                    call.provider_request_id, call.http_status = (
                        exc.provider_request_id,
                        exc.http_status,
                    )
                    call.returned_model, call.usage = exc.returned_model, exc.usage
                    call.provider_fingerprint = exc.provider_fingerprint
            if isinstance(exc, ModelError):
                raise
            raise ModelError(code, retryable=True) from exc
        async with self.db.sessions.begin() as session:
            await self.fenced_item(session, lease)
            call = await session.get(ModelCall, call_id)
            call.status = "succeeded"
            call.duration_ms = int((time.monotonic() - started) * 1000)
            metadata = output if isinstance(output, Evaluation) else output.model_metadata
            if metadata is not None:
                call.returned_model = metadata.returned_model
                call.adapter_version, call.usage = metadata.adapter_version, metadata.usage
                call.provider_request_id = metadata.provider_request_id
                call.provider_fingerprint = metadata.provider_fingerprint
                call.prompt_hash = metadata.prompt_hash or call.prompt_hash
                call.http_status = metadata.http_status
            else:
                call.returned_model = output.extractor_version
                call.adapter_version, call.usage = output.extractor_version, {}
            step = Step(
                item_id=lease.item_id,
                criterion_id=request.criterion.id,
                kind=kind,
                call_id=call.id,
                input_hash=input_hash,
                output=output.model_dump(mode="json"),
            )
            session.add(step)
            await session.flush()
        self.log("checkpoint_saved", lease, criterion_id=request.criterion.id, kind=kind)
        return step

    async def save_assessment(
        self, lease: Lease, request: AssessmentInput, evaluation: Step, evidence: Step
    ) -> None:
        result = Evaluation.model_validate(evaluation.output)
        selected = candidate(result, request.criterion)
        citation = EvidenceOutput.model_validate(evidence.output)
        validate_evidence(request.answer, citation)
        issues = ["requires_teacher_review"]
        if request.profile == "fake-v1":
            issues.append("simulation_not_semantic_grading")
        if selected is None:
            issues.append("abstained")
        if not citation.citations:
            issues.append("no_citation")
        if citation.extractor_version == "manual-evidence-v1":
            issues.append("evidence_pending_manual_selection")
        points = {level.id: level.points for level in request.criterion.levels}
        async with self.db.sessions.begin() as session:
            await self.fenced_item(session, lease)
            existing = await session.scalar(
                select(Assessment.id).where(
                    Assessment.item_id == lease.item_id,
                    Assessment.criterion_id == request.criterion.id,
                )
            )
            if existing is None:
                session.add(
                    Assessment(
                        item_id=lease.item_id,
                        criterion_id=request.criterion.id,
                        level_id=selected,
                        suggested_points=points.get(selected),
                        evaluation_step_id=evaluation.id,
                        evidence_step_id=evidence.id,
                        evidence={
                            **citation.model_dump(mode="json"),
                            "answer_version_id": str(request.answer_version_id),
                            "answer_hash": text_digest(request.answer),
                            "offset_unit": "unicode_code_points",
                            "existence_verified": bool(citation.citations),
                            "selection_status": (
                                "manual_required"
                                if citation.extractor_version == "manual-evidence-v1"
                                else "extracted"
                            ),
                        },
                        issues=issues,
                    )
                )

    async def finish(self, lease: Lease, rubric: Rubric) -> None:
        async with self.db.sessions.begin() as session:
            item = await self.fenced_item(session, lease)
            assessments = list(
                await session.scalars(select(Assessment).where(Assessment.item_id == lease.item_id))
            )
            selected = {a.criterion_id: a.level_id for a in assessments}
            item.suggested_score = calculate_score(rubric, selected)
            item.status = "succeeded"
            item.lease_until, item.lease_owner = None, None
            await session.execute(
                update(Attempt)
                .where(Attempt.item_id == item.id, Attempt.epoch == lease.epoch)
                .values(status="succeeded", finished_at=func.clock_timestamp())
            )
            await session.flush()
            await refresh_batches(session, item.id)
        self.log("succeeded", lease)

    async def fail(
        self,
        lease: Lease,
        code: str,
        *,
        retryable: bool = True,
        retry_after_seconds: int | None = None,
    ) -> None:
        async with self.db.sessions.begin() as session:
            item = await self.fenced_item(session, lease)
            now = await session.scalar(select(func.clock_timestamp()))
            item.last_error = code
            item.status = (
                "failed"
                if item.attempts >= item.max_attempts
                or code == "call_budget_exhausted"
                or not retryable
                else "retry_wait"
            )
            backoff = min(self.settings.retry_delay_seconds * 2 ** min(item.attempts - 1, 10), 3600)
            delay = max(backoff, retry_after_seconds or 0)
            item.ready_at = now + timedelta(seconds=delay)
            item.lease_until, item.lease_owner = None, None
            await session.execute(
                update(Attempt)
                .where(Attempt.item_id == item.id, Attempt.epoch == lease.epoch)
                .values(status="failed", finished_at=now)
            )
            await session.flush()
            await refresh_batches(session, item.id)
        self.log("attempt_failed", lease, error_code=code)

    async def execute(self, lease: Lease) -> None:
        try:
            rubric, requests = await self.context(lease)
            # Validate all frozen inputs before reserving the first potentially billable call.
            for request in requests:
                runtime = GradingRuntime(*self.adapters(request.profile))
                for kind in GRADING_STEPS:
                    runtime.preflight(request, kind)
            for request in requests:
                evaluation = await self.execute_step(lease, request, "evaluate")
                evidence = await self.execute_step(lease, request, "extract_evidence")
                await self.save_assessment(lease, request, evaluation, evidence)
            await self.finish(lease, rubric)
        except LostLease:
            self.log("stale_execution_discarded", lease)
        except (BudgetExhausted, DomainError) as exc:
            code = "call_budget_exhausted" if isinstance(exc, BudgetExhausted) else exc.code
            try:
                await self.fail(
                    lease,
                    code,
                    retryable=exc.retryable if isinstance(exc, ModelError) else True,
                    retry_after_seconds=exc.retry_after_seconds
                    if isinstance(exc, ModelError)
                    else None,
                )
            except LostLease:
                self.log("stale_execution_discarded", lease)
        # Database outages and process crashes leave a recoverable running lease.

    async def run_once(self) -> bool:
        lease = await self.claim()
        if lease is None:
            return False
        await self.execute(lease)
        return True

    async def run_forever(self) -> None:
        failures = 0
        while True:
            try:
                progressed = await self.run_once()
            except Exception as exc:
                # Repeated outages must not dump SQL parameters or model/student
                # payloads into logs. Persisted leases remain the recovery source.
                logger.error("worker_iteration_failed: %s", type(exc).__name__)
                failures += 1
                await asyncio.sleep(failure_delay(self.settings.poll_seconds, failures))
                continue
            failures = 0
            if not progressed:
                await asyncio.sleep(self.settings.poll_seconds)

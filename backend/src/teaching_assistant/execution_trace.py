"""Teacher-scoped, snapshot-consistent view of the persisted grading execution."""

from sqlalchemy import select

from teaching_assistant.db import Attempt, EvaluationProfile, ModelCall, RubricVersion, Step
from teaching_assistant.grading_runtime import GRADING_STEPS
from teaching_assistant.schemas import json_time
from teaching_assistant.service import owned_item


async def item_trace(session, teacher, item_id) -> dict:
    item = await owned_item(session, teacher, item_id)
    profile = await session.get(EvaluationProfile, item.evaluation_profile)
    rubric = await session.get(RubricVersion, item.rubric_version_id)
    attempts = list(
        await session.scalars(
            select(Attempt).where(Attempt.item_id == item.id).order_by(Attempt.epoch)
        )
    )
    calls = list(
        await session.scalars(
            select(ModelCall)
            .where(ModelCall.item_id == item.id)
            .order_by(ModelCall.created_at, ModelCall.id)
        )
    )
    steps = list(
        await session.scalars(
            select(Step).where(Step.item_id == item.id).order_by(Step.criterion_id, Step.kind)
        )
    )
    saved = {(step.criterion_id, step.kind): step for step in steps}
    waiting_for = {
        "pending": "matching_worker",
        "running": "active_attempt_or_lease_recovery",
        "retry_wait": "ready_at_and_matching_worker",
        "failed": "teacher_retry_or_configuration_fix",
        "succeeded": "teacher_review" if item.review_revision == 0 else None,
    }[item.status]
    return {
        "schema_version": "grading-execution-trace-v1",
        "item_id": str(item.id),
        "status": item.status,
        "error_code": item.last_error,
        "waiting_for": waiting_for,
        "ready_at": json_time(item.ready_at),
        "lease_until": json_time(item.lease_until),
        "epoch": item.epoch,
        "answer_version_id": str(item.answer_version_id),
        "question_version_id": str(rubric.question_version_id),
        "rubric_version_id": str(item.rubric_version_id),
        "profile": {
            "id": profile.id,
            "fingerprint": profile.fingerprint,
            "manifest": profile.manifest,
        },
        "budget": {
            "attempts_used": item.attempts,
            "max_attempts": item.max_attempts,
            "calls_used": item.calls_used,
            "max_calls": item.max_calls,
            "calls_remaining": max(0, item.max_calls - item.calls_used),
            "unit": "adapter_invocations_including_local_evidence",
        },
        "plan": [
            {
                "criterion_id": criterion["id"],
                "kind": kind,
                "status": "checkpointed"
                if (criterion["id"], kind) in saved
                else "not_checkpointed",
                "step_id": str(saved[criterion["id"], kind].id)
                if (criterion["id"], kind) in saved
                else None,
            }
            for criterion in rubric.body["criteria"]
            for kind in GRADING_STEPS
        ],
        "attempts": [
            {
                "id": str(attempt.id),
                "epoch": attempt.epoch,
                "status": attempt.status,
                "created_at": json_time(attempt.created_at),
                "finished_at": json_time(attempt.finished_at),
            }
            for attempt in attempts
        ],
        "calls": [
            {
                "id": str(call.id),
                "epoch": call.epoch,
                "criterion_id": call.criterion_id,
                "kind": call.kind,
                "status": call.status,
                "input_hash": call.input_hash,
                "created_at": json_time(call.created_at),
                "duration_ms": call.duration_ms,
                "requested_model": call.requested_model,
                "returned_model": call.returned_model,
                "adapter_version": call.adapter_version,
                "prompt_hash": call.prompt_hash,
                "usage": call.usage,
                "provider_request_id": call.provider_request_id,
                "provider_fingerprint": call.provider_fingerprint,
                "http_status": call.http_status,
                "error_code": call.error_code,
                "error_retryable": call.error_retryable,
            }
            for call in calls
        ],
        "checkpoints": [
            {
                "id": str(step.id),
                "criterion_id": step.criterion_id,
                "kind": step.kind,
                "call_id": str(step.call_id),
                "input_hash": step.input_hash,
                "created_at": json_time(step.created_at),
                "output": step.output,
            }
            for step in steps
        ],
        "notice": "Snapshot only; unknown calls may have been billed. Checkpoints do not confirm grades.",
    }

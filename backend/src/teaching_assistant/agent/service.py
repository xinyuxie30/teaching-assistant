from datetime import timedelta
from uuid import UUID

from sqlalchemy import func, select

from teaching_assistant import service
from teaching_assistant.agent.context import initial_messages, manifest, payload, tool_result
from teaching_assistant.db import AgentEvent, AgentRun, RubricDraft
from teaching_assistant.domain import DomainError, ModelError, digest
from teaching_assistant.rubric_diff import compare_rubrics
from teaching_assistant.schemas import json_time


def profiles(settings):
    result = []
    for alias in ("fake-rubric-agent-v1", "deepseek-rubric-agent-v1"):
        try:
            policy = manifest(settings, alias)
        except ModelError:
            result.append({"alias": alias, "id": None, "ready": False})
            continue
        ready = policy["provider"] == "fake" or bool(
            settings.deepseek_api_key and settings.deepseek_api_key.get_secret_value().strip()
        )
        result.append(
            {"alias": alias, "id": "agent-" + digest(policy), "manifest": policy, "ready": ready}
        )
    return result


def event(session, run, kind, **body):
    run.event_seq += 1
    session.add(
        AgentEvent(run_id=run.id, sequence=run.event_seq, epoch=run.epoch, kind=kind, body=body)
    )
    return run.event_seq


def close_inflight(session, run, code):
    if run.inflight_seq is not None:
        event(
            session,
            run,
            "model_unknown",
            call_sequence=run.inflight_seq,
            error_code=code,
            usage=None,
        )
        run.inflight_seq = None


def close_pending(session, run, code):
    for call in run.pending_calls:
        run.messages = [*run.messages, tool_result(call["id"], {"ok": False, "error_code": code})]
        event(session, run, "tool_skipped", tool_call_id=call["id"], error_code=code)
    run.pending_calls = []


def finish(session, run, status, code=None):
    close_inflight(session, run, code or status)
    close_pending(session, run, code or status)
    run.status, run.last_error = status, code
    run.lease_owner, run.lease_until = None, None
    event(
        session, run, status, error_code=code, draft_id=str(run.draft_id) if run.draft_id else None
    )


async def owned_run(session, teacher, run_id, *, lock=False):
    query = select(AgentRun).where(AgentRun.id == run_id, AgentRun.teacher_id == teacher)
    if lock:
        query = query.with_for_update()
    run = await session.scalar(query)
    if run is None:
        raise DomainError("not_found", "Agent run not found", 404)
    return run


async def list_runs(session, teacher, params):
    from teaching_assistant.listings import page

    if params.question_version_id is not None:
        await service.owned_question(session, teacher, params.question_version_id)
    query = select(AgentRun.id, AgentRun.created_at).where(AgentRun.teacher_id == teacher)
    if params.question_version_id is not None:
        query = query.where(AgentRun.question_version_id == params.question_version_id)
    if params.status is not None:
        query = query.where(AgentRun.status == params.status)
    result = await page(session, query, AgentRun, teacher, "agent-runs", params)
    ids = [UUID(row["id"]) for row in result["items"]]
    # Both the page and its summaries use the caller's repeatable-read snapshot.
    rows = await session.scalars(select(AgentRun).where(AgentRun.id.in_(ids)))
    by_id = {row.id: row for row in rows}
    result["items"] = [view(by_id[run_id]) for run_id in ids]
    return result


async def create_run(session, teacher, question_id, data, settings):
    await service.owned_question(session, teacher, question_id)
    revision_context = None
    if data.source_draft_id is not None:
        source = await session.scalar(
            select(RubricDraft)
            .where(
                RubricDraft.id == data.source_draft_id,
                RubricDraft.question_version_id == question_id,
            )
            .with_for_update()
        )
        if source is None:
            raise DomainError("not_found", "Source draft not found for this question", 404)
        revision_context = {
            "version": "rubric-revision-context-v1",
            "feedback": data.instructions,
            "source_revision": source.revision,
            "source_content_hash": digest(source.body),
            "source_rubric": source.body,
        }
    selected = next(p for p in profiles(settings) if p["alias"] == data.agent_profile)
    if not selected["ready"]:
        raise DomainError("model_not_configured", "Configure the selected agent model first", 409)
    limits = data.model_dump(exclude={"agent_profile", "instructions", "source_draft_id"})
    messages = initial_messages(data.instructions, revision_context)
    payload(messages, selected["manifest"], limits)
    now = await session.scalar(select(func.clock_timestamp()))
    run = AgentRun(
        teacher_id=teacher,
        question_version_id=question_id,
        source_draft_id=data.source_draft_id,
        revision_context=revision_context,
        profile_id=selected["id"],
        manifest=selected["manifest"],
        limits=limits,
        messages=messages,
        expires_at=now + timedelta(seconds=data.max_seconds),
    )
    session.add(run)
    await session.flush()
    source = revision_source(run)
    event(
        session,
        run,
        "created",
        question_version_id=str(question_id),
        profile_id=run.profile_id,
        source_draft=source,
    )
    service.audit(
        session,
        teacher,
        "rubric_agent_created",
        run.id,
        question_version_id=str(question_id),
        source_draft=source,
    )
    return view(run)


def revision_source(run):
    if run.source_draft_id is None:
        return None
    return {
        "id": str(run.source_draft_id),
        "revision": run.revision_context["source_revision"],
        "content_hash": run.revision_context["source_content_hash"],
    }


async def draft_revision_comparison(session, teacher, draft):
    """Called inside the authorized draft detail's repeatable-read snapshot."""
    run = await session.scalar(
        select(AgentRun).where(
            AgentRun.teacher_id == teacher,
            AgentRun.question_version_id == draft.question_version_id,
            AgentRun.draft_id == draft.id,
            AgentRun.source_draft_id.is_not(None),
        )
    )
    if run is None:
        return None
    source_body = run.revision_context["source_rubric"]
    return {
        "run_id": str(run.id),
        "source": revision_source(run),
        "feedback": run.revision_context["feedback"],
        "source_body": source_body,
        "target": {
            "id": str(draft.id),
            "revision": draft.revision,
            "content_hash": digest(draft.body),
        },
        "difference": compare_rubrics(source_body, draft.body),
    }


def view(run, *, detail=False):
    return {
        "id": str(run.id),
        "question_version_id": str(run.question_version_id),
        "status": run.status,
        "error_code": run.last_error,
        "draft_id": str(run.draft_id) if run.draft_id else None,
        "source_draft": revision_source(run),
        **(
            {"revision_feedback": run.revision_context["feedback"]}
            if detail and run.revision_context
            else {}
        ),
        "waiting_for": "teacher_rubric_confirmation" if run.status == "waiting_teacher" else None,
        "profile_id": run.profile_id,
        "manifest": run.manifest,
        "simulation": run.manifest["provider"] == "fake",
        "limits": run.limits,
        "models_used": run.models_used,
        "tools_used": run.tools_used,
        "attempts": run.attempts,
        "epoch": run.epoch,
        "event_sequence": run.event_seq,
        "expires_at": json_time(run.expires_at),
        "created_at": json_time(run.created_at),
    }


async def cancel_run(session, teacher, run_id, data):
    run = await owned_run(session, teacher, run_id, lock=True)
    if run.status == "cancelled":
        return view(run)
    if run.status not in {"queued", "running"}:
        raise DomainError(
            "agent_not_cancellable", "Run is already terminal or waiting for review", 409
        )
    finish(session, run, "cancelled", "teacher_cancelled")
    run.epoch += 1  # Immediately fence any in-flight model response and subsequent tool writes.
    service.audit(session, teacher, "rubric_agent_cancelled", run.id, reason=data.reason)
    return view(run)


async def rubric_confirmed(session, teacher, draft_id, version_id):
    """Handle the explicit teacher action in the same transaction as rubric freezing."""
    runs = await session.scalars(
        select(AgentRun)
        .where(
            AgentRun.teacher_id == teacher,
            AgentRun.draft_id == draft_id,
            AgentRun.status == "waiting_teacher",
        )
        .order_by(AgentRun.id)
        .with_for_update()
    )
    for run in runs:
        event(session, run, "teacher_confirmed", rubric_version_id=str(version_id))
        finish(session, run, "completed")

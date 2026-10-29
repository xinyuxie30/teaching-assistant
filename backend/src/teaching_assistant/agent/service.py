from datetime import timedelta
from uuid import UUID

from sqlalchemy import func, select

from teaching_assistant import service
from teaching_assistant.agent.context import initial_messages, payload
from teaching_assistant.db import AgentArtifact, AgentRun, AgentWait, AgentWake, RubricDraft
from teaching_assistant.domain import DomainError, digest
from teaching_assistant.harness import budgets, catalog, scheduler, waiting
from teaching_assistant.harness.context import initial_messages as task_messages
from teaching_assistant.harness.context import payload as task_payload
from teaching_assistant.harness.grading import GradingSnapshot, validate_scope
from teaching_assistant.harness.store import event as event
from teaching_assistant.harness.store import finish as finish
from teaching_assistant.profiles import ProfileRegistry
from teaching_assistant.rubric_diff import compare_rubrics
from teaching_assistant.schemas import json_time


def profiles(settings, *, task_type="rubric"):
    return catalog.profiles(settings, task_type=task_type)


async def create_grading_run(session, teacher, data, settings):
    frozen = await validate_scope(session, teacher, data)
    grading = ProfileRegistry(settings).resolve(data.evaluation_profile)
    if grading.manifest["provider"] != "typesafe":
        raise DomainError("jev_profile_required", "This grading tool requires a pinned Jev profile")
    task = catalog.resolve("homework-grading")
    selected = next(
        p for p in profiles(settings, task_type=task.key) if p["alias"] == data.agent_profile
    )
    if not selected["ready"]:
        raise DomainError("model_not_configured", "Configure the controller model first", 409)
    limits = data.model_dump(
        mode="json",
        exclude_none=True,
        exclude={
            "agent_profile",
            "instructions",
            "rubric_version_id",
            "answer_version_ids",
            "evaluation_profile",
        },
    )
    budgets.validate_limits(limits, selected["manifest"])
    messages = task_messages(task.system, data.instructions)
    task_payload(messages, selected["manifest"], limits, task.registry.schemas())
    now = await session.scalar(select(func.clock_timestamp()))
    run = AgentRun(
        teacher_id=teacher,
        task_type=task.key,
        question_version_id=frozen.question_version_id,
        task_input=GradingSnapshot(
            rubric_version_id=frozen.id,
            answer_version_ids=data.answer_version_ids,
            evaluation_profile=grading.id,
            evaluation_manifest=grading.manifest,
        ).model_dump(mode="json"),
        profile_id=selected["id"],
        manifest=selected["manifest"],
        limits=limits,
        messages=messages,
        expires_at=now + timedelta(seconds=data.max_seconds),
    )
    session.add(run)
    await session.flush()
    event(
        session,
        run,
        "created",
        task_type=task.key,
        profile_id=run.profile_id,
        input_hash=digest(run.task_input),
        grading_profile_id=grading.id,
    )
    service.audit(
        session,
        teacher,
        "grading_agent_created",
        run.id,
        grading_profile_id=grading.id,
        answer_count=len(data.answer_version_ids),
    )
    return view(run)


async def create_lesson_run(session, teacher, course_id, data, settings):
    await service.owned_course(session, teacher, course_id)
    task = catalog.resolve(
        next(
            key
            for key in (
                "lesson-plan",
                "lesson-plan-team",
                "lesson-plan-parallel",
                "lesson-plan-assisted",
                "lesson-plan-research",
                "lesson-plan-background",
            )
            if data.agent_profile in catalog.ALIASES.get(key, ())
        )
    )
    selected = next(
        p for p in profiles(settings, task_type=task.key) if p["alias"] == data.agent_profile
    )
    if not selected["ready"]:
        raise DomainError("model_not_configured", "Configure the selected agent model first", 409)
    limits = data.model_dump(
        mode="json", exclude_none=True, exclude={"agent_profile", "instructions", "brief"}
    )
    budgets.validate_limits(limits, selected["manifest"])
    messages = task_messages(task.system, data.instructions)
    task_payload(messages, selected["manifest"], limits, task.registry.schemas())
    now = await session.scalar(select(func.clock_timestamp()))
    run = AgentRun(
        teacher_id=teacher,
        task_type=task.key,
        task_input={"course_id": str(course_id), "brief": data.brief.model_dump(mode="json")},
        profile_id=selected["id"],
        manifest=selected["manifest"],
        limits=limits,
        messages=messages,
        expires_at=now + timedelta(seconds=data.max_seconds),
    )
    session.add(run)
    await session.flush()
    event(
        session,
        run,
        "created",
        task_type=task.key,
        profile_id=run.profile_id,
        input_hash=digest(run.task_input),
    )
    service.audit(session, teacher, "lesson_agent_created", run.id, course_id=str(course_id))
    return view(run)


async def owned_artifact(session, teacher, run_id, *, lock=False):
    run = await owned_run(session, teacher, run_id, lock=lock)
    await catalog.resolve(run.task_type).authorize(session, run)
    artifact = await session.scalar(select(AgentArtifact).where(AgentArtifact.run_id == run.id))
    if artifact is None:
        raise DomainError("not_found", "This run has no generic artifact", 404)
    return run, artifact


def artifact_view(artifact):
    return {
        "id": str(artifact.id),
        "run_id": str(artifact.run_id),
        "kind": artifact.kind,
        "body": artifact.body,
        "content_hash": artifact.content_hash,
        "created_at": json_time(artifact.created_at),
        "confirmed_at": json_time(artifact.confirmed_at),
    }


async def confirm_artifact(session, teacher, run_id, data):
    run, artifact = await owned_artifact(session, teacher, run_id, lock=True)
    task = catalog.resolve(run.task_type)
    if artifact.content_hash != data.content_hash or not await task.artifact_exists(session, run):
        raise DomainError("artifact_changed", "Review the saved artifact before confirming", 409)
    if run.status == "completed" and artifact.confirmed_at is not None:
        return view(run)
    if run.status != "waiting_teacher" or artifact.confirmed_at is not None:
        raise DomainError(
            "agent_not_confirmable", "Run is not waiting for teacher confirmation", 409
        )
    artifact.confirmed_at = await session.scalar(select(func.clock_timestamp()))
    event(
        session,
        run,
        "teacher_confirmed",
        artifact_id=str(artifact.id),
        content_hash=data.content_hash,
    )
    service.audit(session, teacher, "agent_artifact_confirmed", artifact.id, run_id=str(run.id))
    finish(session, run, "completed")
    return view(run)


async def owned_run(session, teacher, run_id, *, lock=False):
    query = select(AgentRun).where(AgentRun.id == run_id, AgentRun.teacher_id == teacher)
    if lock:
        # Authorize before taking tree locks; then use the same root-first order as Workers.
        if await session.scalar(query) is None:
            raise DomainError("not_found", "Agent run not found", 404)
        _, run = await scheduler.lock_run(session, run_id)
        if run is None or run.teacher_id != teacher:
            raise DomainError("not_found", "Agent run not found", 404)
        return run
    run = await session.scalar(query)
    if run is None:
        raise DomainError("not_found", "Agent run not found", 404)
    return run


async def list_children(session, teacher, run_id):
    run = await owned_run(session, teacher, run_id)
    rows = await session.scalars(
        select(AgentRun)
        .where(AgentRun.parent_run_id == run.id, AgentRun.teacher_id == teacher)
        .order_by(AgentRun.created_at, AgentRun.id)
    )
    return {"items": [view(row) for row in rows]}


async def context_checkpoints(session, teacher, run_id):
    from teaching_assistant.db import AgentContextCheckpoint

    run = await owned_run(session, teacher, run_id)
    rows = await session.scalars(
        select(AgentContextCheckpoint)
        .where(AgentContextCheckpoint.run_id == run.id)
        .order_by(AgentContextCheckpoint.sequence)
    )
    # The full transcript remains internal; report verifiable compression receipts.
    return {
        "items": [
            {
                "sequence": row.sequence,
                "source_hash": row.source_hash,
                "details": row.details,
                "created_at": json_time(row.created_at),
            }
            for row in rows
        ]
    }


async def list_runs(session, teacher, params):
    from teaching_assistant.listings import page

    if params.question_version_id is not None:
        await service.owned_question(session, teacher, params.question_version_id)
    query = select(AgentRun.id, AgentRun.created_at).where(AgentRun.teacher_id == teacher)
    if params.question_version_id is not None:
        query = query.where(AgentRun.question_version_id == params.question_version_id)
    if params.task_type is not None:
        query = query.where(AgentRun.task_type == params.task_type)
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
    limits = data.model_dump(
        mode="json", exclude_none=True, exclude={"agent_profile", "instructions", "source_draft_id"}
    )
    budgets.validate_limits(limits, selected["manifest"])
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
        **(
            {"grading_batch_id": (run.task_state or {}).get("batch_id")}
            if run.task_type == "homework-grading"
            else {}
        ),
        "id": str(run.id),
        "question_version_id": str(run.question_version_id) if run.question_version_id else None,
        "task_type": run.task_type,
        "parent_run_id": str(run.parent_run_id) if run.parent_run_id else None,
        "root_run_id": str(run.root_run_id or run.id),
        "depth": run.depth,
        "status": run.status,
        "error_code": run.last_error,
        "draft_id": str(run.draft_id) if run.draft_id else None,
        "source_draft": revision_source(run),
        **(
            {"revision_feedback": run.revision_context["feedback"]}
            if detail and run.revision_context
            else {}
        ),
        "waiting_for": (
            catalog.resolve(run.task_type).waiting_for if run.status == "waiting_teacher" else None
        ),
        "wait": run.wait_state or None,
        "profile_id": run.profile_id,
        "manifest": run.manifest,
        "simulation": run.manifest["provider"] == "fake",
        "limits": run.limits,
        "models_used": run.models_used,
        "tools_used": run.tools_used,
        "tree_models_used": run.tree_models_used if run.parent_run_id is None else None,
        "tree_tools_used": run.tree_tools_used if run.parent_run_id is None else None,
        "children_spawned": run.children_spawned,
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
    if run.status not in (*scheduler.ACTIVE, *waiting.WAITING):
        raise DomainError(
            "agent_not_cancellable", "Run is already terminal or waiting for review", 409
        )
    await waiting.cancel_wait(session, run)
    finish(session, run, "cancelled", "teacher_cancelled")
    run.epoch += 1  # Immediately fence any in-flight model response and subsequent tool writes.
    run.resume_pending = False
    await scheduler.cancel_descendants(session, run, "parent_cancelled")
    service.audit(
        session, teacher, "agent_cancelled", run.id, task_type=run.task_type, reason=data.reason
    )
    return view(run)


async def wait_run(session, teacher, run_id, data):
    run = await owned_run(session, teacher, run_id, lock=True)
    await catalog.resolve(run.task_type).authorize(session, run)
    await waiting.pause_tree(session, run, reason=data.reason, wake_at=data.wake_at)
    service.audit(session, teacher, "agent_paused", run.id, wait_token=run.wait_state["token"])
    return view(run)


async def wake_run(session, teacher, run_id, data):
    run = await owned_run(session, teacher, run_id, lock=True)
    await catalog.resolve(run.task_type).authorize(session, run)
    changed = await waiting.wake_tree(session, run, data.wait_token, response=data.response)
    if changed:
        service.audit(session, teacher, "agent_woken", run.id, wait_token=str(data.wait_token))
    return view(run)


async def list_waits(session, teacher, run_id):
    run = await owned_run(session, teacher, run_id)
    rows = (
        await session.execute(
            select(AgentWait, AgentWake)
            .outerjoin(AgentWake, AgentWake.wait_id == AgentWait.id)
            .where(AgentWait.run_id == run.id)
            .order_by(AgentWait.created_at, AgentWait.id)
        )
    ).all()
    return {
        "items": [
            {
                "wait_token": str(saved.id),
                "kind": saved.kind,
                "reason": saved.reason,
                "wake_at": json_time(saved.wake_at),
                "created_at": json_time(saved.created_at),
                "snapshot_hash": saved.content_hash,
                "question": saved.snapshot.get("question"),
                "resolved_at": json_time(receipt.created_at) if receipt else None,
                "resolved_by": receipt.source if receipt else None,
                "response": receipt.response if receipt else None,
            }
            for saved, receipt in rows
        ]
    }


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

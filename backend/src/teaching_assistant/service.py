"""Transactional application operations. All external model work lives outside these transactions."""

import asyncio
import hashlib
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from teaching_assistant.db import (
    AnswerVersion,
    Assessment,
    Assignment,
    AssignmentMember,
    Audit,
    BatchItem,
    Classroom,
    Course,
    Database,
    EvaluationProfile,
    ExportSnapshot,
    GradingBatch,
    GradingItem,
    ImportRecord,
    Membership,
    ModelCall,
    QuestionVersion,
    Receipt,
    Review,
    RubricDraft,
    RubricVersion,
    Step,
    Student,
)
from teaching_assistant.domain import (
    DomainError,
    Evidence,
    EvidenceOutput,
    Rubric,
    calculate_score,
    digest,
    text_digest,
    validate_evidence,
)
from teaching_assistant.profiles import Profile, fake_profile
from teaching_assistant.schemas import (
    AssignmentInput,
    BatchInput,
    ConfirmInput,
    DraftInput,
    ExportInput,
    ImportInput,
    QuestionInput,
    RetryInput,
    ReviewInput,
    WorkspaceInput,
    json_time,
)


async def transaction_lock(session: AsyncSession, name: str) -> None:
    key = int.from_bytes(hashlib.sha256(name.encode()).digest()[:8], "big", signed=True)
    await session.execute(select(func.pg_advisory_xact_lock(key)))


async def write_operation(
    db: Database,
    teacher_id: UUID,
    scope: str,
    key: str,
    payload: dict,
    action: Callable[[AsyncSession], Awaitable[dict]],
    *,
    snapshot: bool = False,
) -> dict:
    """Commit the business mutation and its replayable receipt together."""
    if not key or len(key) > 120:
        raise DomainError(
            "invalid_idempotency_key", "Idempotency-Key must contain 1–120 characters"
        )
    request_hash = digest(payload)
    factory = db.snapshot_sessions if snapshot else db.sessions
    for retry in range(3):
        try:
            async with factory.begin() as session:
                receipt_id = uuid4()
                result = await session.execute(
                    insert(Receipt)
                    .values(
                        id=receipt_id,
                        teacher_id=teacher_id,
                        scope=scope,
                        key=key,
                        request_hash=request_hash,
                    )
                    .on_conflict_do_nothing(index_elements=["teacher_id", "scope", "key"])
                    .returning(Receipt.id)
                )
                inserted = result.scalar_one_or_none()
                if inserted is None:
                    receipt = await session.scalar(
                        select(Receipt).where(
                            Receipt.teacher_id == teacher_id,
                            Receipt.scope == scope,
                            Receipt.key == key,
                        )
                    )
                    if receipt is None:
                        raise DomainError("snapshot_conflict", "Retry the same request", 409)
                    if receipt.request_hash != request_hash:
                        raise DomainError(
                            "idempotency_conflict", "Key was used with another payload", 409
                        )
                    return receipt.response
                response = await action(session)
                receipt = await session.get(Receipt, receipt_id)
                receipt.response = response
                return response
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) not in {"40001", "40P01"} or retry == 2:
                raise
            await asyncio.sleep(0.02 * (retry + 1))
    raise AssertionError("unreachable")


async def owned_course(session: AsyncSession, teacher: UUID, course_id: UUID) -> Course:
    course = await session.scalar(
        select(Course).where(Course.id == course_id, Course.teacher_id == teacher)
    )
    if course is None:
        raise DomainError("not_found", "Course not found", 404)
    return course


async def owned_assignment(session: AsyncSession, teacher: UUID, assignment_id: UUID) -> Assignment:
    assignment = await session.scalar(
        select(Assignment)
        .join(Course)
        .where(Assignment.id == assignment_id, Course.teacher_id == teacher)
    )
    if assignment is None:
        raise DomainError("not_found", "Assignment not found", 404)
    return assignment


async def owned_question(
    session: AsyncSession, teacher: UUID, question_id: UUID
) -> QuestionVersion:
    question = await session.get(QuestionVersion, question_id)
    if question is None:
        raise DomainError("not_found", "Question version not found", 404)
    await owned_assignment(session, teacher, question.assignment_id)
    return question


async def owned_rubric(session: AsyncSession, teacher: UUID, rubric_id: UUID) -> RubricVersion:
    rubric = await session.get(RubricVersion, rubric_id)
    if rubric is None:
        raise DomainError("not_found", "Rubric version not found", 404)
    await owned_question(session, teacher, rubric.question_version_id)
    return rubric


async def owned_item(
    session: AsyncSession, teacher: UUID, item_id: UUID, *, lock: bool = False
) -> GradingItem:
    query = select(GradingItem).where(GradingItem.id == item_id, GradingItem.teacher_id == teacher)
    if lock:
        query = query.with_for_update()
    item = await session.scalar(query)
    if item is None:
        raise DomainError("not_found", "Grading item not found", 404)
    return item


async def owned_batch(session: AsyncSession, teacher: UUID, batch_id: UUID) -> GradingBatch:
    batch = await session.scalar(
        select(GradingBatch).where(GradingBatch.id == batch_id, GradingBatch.teacher_id == teacher)
    )
    if batch is None:
        raise DomainError("not_found", "Batch not found", 404)
    return batch


def audit(session: AsyncSession, teacher: UUID, action: str, object_id: UUID, **details):
    session.add(Audit(teacher_id=teacher, action=action, object_id=object_id, details=details))


async def create_workspace(session: AsyncSession, teacher: UUID, data: WorkspaceInput) -> dict:
    course = Course(teacher_id=teacher, name=data.course_name)
    session.add(course)
    await session.flush()
    classroom = Classroom(course_id=course.id, name=data.classroom_name)
    session.add(classroom)
    students = [
        Student(course_id=course.id, external_id=s.external_id, name=s.name) for s in data.students
    ]
    session.add_all(students)
    await session.flush()
    session.add_all(Membership(classroom_id=classroom.id, student_id=s.id) for s in students)
    audit(session, teacher, "workspace_created", course.id)
    return {
        "course_id": str(course.id),
        "classroom_id": str(classroom.id),
        "students": [
            {"id": str(s.id), "external_id": s.external_id, "name": s.name} for s in students
        ],
    }


async def create_assignment(
    session: AsyncSession, teacher: UUID, course_id: UUID, data: AssignmentInput
) -> dict:
    await owned_course(session, teacher, course_id)
    classroom = await session.get(Classroom, data.classroom_id)
    if classroom is None or classroom.course_id != course_id:
        raise DomainError("not_found", "Classroom not found in course", 404)
    assignment = Assignment(course_id=course_id, classroom_id=classroom.id, title=data.title)
    session.add(assignment)
    await session.flush()
    members = list(
        await session.scalars(
            select(Membership.student_id).where(Membership.classroom_id == classroom.id)
        )
    )
    session.add_all(AssignmentMember(assignment_id=assignment.id, student_id=s) for s in members)
    audit(session, teacher, "assignment_created", assignment.id, roster_size=len(members))
    return {"id": str(assignment.id), "roster_size": len(members)}


async def create_question(
    session: AsyncSession, teacher: UUID, assignment_id: UUID, data: QuestionInput
) -> dict:
    await owned_assignment(session, teacher, assignment_id)
    await session.execute(
        select(Assignment.id).where(Assignment.id == assignment_id).with_for_update()
    )
    version = (
        await session.scalar(
            select(func.max(QuestionVersion.version)).where(
                QuestionVersion.assignment_id == assignment_id,
                QuestionVersion.question_key == data.question_key,
            )
        )
        or 0
    ) + 1
    question = QuestionVersion(
        assignment_id=assignment_id,
        version=version,
        **data.model_dump(),
        content_hash=digest(data.model_dump(mode="json")),
    )
    session.add(question)
    await session.flush()
    audit(session, teacher, "question_version_created", question.id, version=version)
    return {"id": str(question.id), "version": version, "content_hash": question.content_hash}


async def create_draft(
    session: AsyncSession, teacher: UUID, question_id: UUID, data: DraftInput
) -> dict:
    question = await owned_question(session, teacher, question_id)
    if data.rubric.maximum_points != question.maximum_points:
        raise DomainError("rubric_total_mismatch", "Criterion maxima must sum to question maximum")
    draft = RubricDraft(question_version_id=question_id, body=data.rubric.model_dump(mode="json"))
    session.add(draft)
    await session.flush()
    return {"id": str(draft.id), "revision": draft.revision, "rubric": draft.body}


async def confirm_draft(
    session: AsyncSession, teacher: UUID, draft_id: UUID, data: ConfirmInput
) -> dict:
    draft = await session.scalar(
        select(RubricDraft).where(RubricDraft.id == draft_id).with_for_update()
    )
    if draft is None:
        raise DomainError("not_found", "Draft not found", 404)
    question = await owned_question(session, teacher, draft.question_version_id)
    if draft.revision != data.expected_revision:
        raise DomainError("revision_conflict", "Draft revision changed", 409)
    if draft.confirmed_version_id is not None:
        frozen = await session.get(RubricVersion, draft.confirmed_version_id)
    else:
        rubric = Rubric.model_validate(draft.body)
        if rubric.maximum_points != question.maximum_points:
            raise DomainError("rubric_total_mismatch", "Question and rubric maxima differ")
        await session.execute(
            select(QuestionVersion.id).where(QuestionVersion.id == question.id).with_for_update()
        )
        version = (
            await session.scalar(
                select(func.max(RubricVersion.version)).where(
                    RubricVersion.question_version_id == question.id
                )
            )
            or 0
        ) + 1
        frozen = RubricVersion(
            question_version_id=question.id,
            version=version,
            body=draft.body,
            content_hash=digest(draft.body),
            confirmed_by=teacher,
        )
        session.add(frozen)
        await session.flush()
        draft.confirmed_version_id = frozen.id
        audit(session, teacher, "rubric_confirmed", frozen.id, draft_id=str(draft.id))
    from teaching_assistant.agent.service import rubric_confirmed

    await rubric_confirmed(session, teacher, draft.id, frozen.id)
    return {"id": str(frozen.id), "version": frozen.version, "content_hash": frozen.content_hash}


async def validate_import(
    session: AsyncSession, teacher: UUID, assignment_id: UUID, data: ImportInput
) -> list[AnswerVersion | None]:
    await owned_assignment(session, teacher, assignment_id)
    members = set(
        await session.scalars(
            select(AssignmentMember.student_id).where(
                AssignmentMember.assignment_id == assignment_id
            )
        )
    )
    question_ids = set(
        await session.scalars(
            select(QuestionVersion.id).where(QuestionVersion.assignment_id == assignment_id)
        )
    )
    for row in data.rows:
        if row.student_id not in members or row.question_version_id not in question_ids:
            raise DomainError(
                "invalid_import_scope", "Student or question is outside this assignment"
            )

    def identity(row):
        return (
            row.question_version_id,
            row.student_id,
            row.external_submission_id,
            row.source_revision,
        )

    # The input is bounded to 200 rows. Match the full composite identity in one
    # query; independent IN predicates would also fetch unintended combinations.
    stored = {
        identity(row): row
        for row in await session.scalars(
            select(AnswerVersion).where(
                tuple_(
                    AnswerVersion.question_version_id,
                    AnswerVersion.student_id,
                    AnswerVersion.external_submission_id,
                    AnswerVersion.source_revision,
                ).in_([identity(row) for row in data.rows])
            )
        )
    }
    existing_rows = []
    for row in data.rows:
        existing = stored.get(identity(row))
        if existing is not None and (
            existing.answer_text != row.answer_text or existing.submitted_at != row.submitted_at
        ):
            raise DomainError(
                "submission_revision_conflict",
                "The same source revision has different content or submitted_at",
                409,
            )
        existing_rows.append(existing)
    return existing_rows


async def import_answers(
    session: AsyncSession, teacher: UUID, assignment_id: UUID, data: ImportInput
) -> dict:
    await owned_assignment(session, teacher, assignment_id)
    # Serializes source-revision deduplication for this assignment, including different API keys.
    await session.execute(
        select(Assignment.id).where(Assignment.id == assignment_id).with_for_update()
    )
    existing_rows = await validate_import(session, teacher, assignment_id, data)
    answers = []
    reused = 0
    for row, existing in zip(data.rows, existing_rows, strict=True):
        if existing is None:
            existing = AnswerVersion(**row.model_dump(), content_hash=text_digest(row.answer_text))
            session.add(existing)
        else:
            reused += 1
        answers.append(existing)
    await session.flush()
    answer_ids = [str(answer.id) for answer in answers]
    record = ImportRecord(
        assignment_id=assignment_id,
        teacher_id=teacher,
        request_hash=digest(data.model_dump(mode="json")),
        answer_version_ids=answer_ids,
    )
    session.add(record)
    await session.flush()
    audit(session, teacher, "answers_imported", record.id, count=len(answers), reused=reused)
    return {"id": str(record.id), "answer_version_ids": answer_ids, "reused": reused}


async def refresh_batches(session: AsyncSession, item_id: UUID) -> None:
    batch_ids = list(
        await session.scalars(
            select(BatchItem.batch_id)
            .where(BatchItem.item_id == item_id)
            .order_by(BatchItem.batch_id)
        )
    )
    for batch_id in batch_ids:
        await refresh_batch(session, batch_id)


async def refresh_batch(session: AsyncSession, batch_id: UUID) -> None:
    # Lock first, then issue a fresh SELECT: a pre-lock ORM snapshot can lose another item's update.
    batch = await session.scalar(
        select(GradingBatch)
        .where(GradingBatch.id == batch_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    rows = (
        await session.execute(
            select(
                GradingItem.status,
                func.count().label("total"),
                func.count().filter(GradingItem.review_revision > 0).label("confirmed"),
            )
            .join(BatchItem, BatchItem.item_id == GradingItem.id)
            .where(BatchItem.batch_id == batch_id)
            .group_by(GradingItem.status)
        )
    ).all()
    counts = {row.status: row.total for row in rows}
    counts["total"] = sum(row.total for row in rows)
    counts["confirmed"] = sum(row.confirmed for row in rows)
    counts["awaiting_review"] = sum(
        row.total - row.confirmed for row in rows if row.status == "succeeded"
    )
    batch.counts = counts
    terminal = counts.get("succeeded", 0) + counts.get("failed", 0)
    if terminal == counts["total"]:
        batch.status = "settled"
        batch.outcome = (
            "all_succeeded"
            if counts.get("failed", 0) == 0
            else "all_failed"
            if counts.get("succeeded", 0) == 0
            else "partial_failure"
        )
    else:
        batch.status = "queued" if counts.get("pending", 0) == counts["total"] else "running"
        batch.outcome = None


async def create_batch(
    session: AsyncSession, teacher: UUID, data: BatchInput, *, profile: Profile | None = None
) -> dict:
    frozen = await owned_rubric(session, teacher, data.rubric_version_id)
    if profile is None:
        if data.evaluation_profile != "fake-v1":
            raise DomainError(
                "unknown_evaluation_profile", "Resolve the profile before creating work"
            )
        profile = fake_profile()
    await session.execute(
        insert(EvaluationProfile)
        .values(
            id=profile.id,
            manifest=profile.manifest,
            fingerprint=profile.fingerprint,
        )
        # Concurrent first registrations can conflict on either unique index.
        # Verify identity and content below; never update or alias a frozen profile.
        .on_conflict_do_nothing()
    )
    stored_profile = await session.get(EvaluationProfile, profile.id)
    if (
        stored_profile is None
        or stored_profile.fingerprint != profile.fingerprint
        or stored_profile.manifest != profile.manifest
    ):
        raise DomainError(
            "evaluation_profile_conflict", "An existing profile has different content", 409
        )
    rubric = Rubric.model_validate(frozen.body)
    answers = (
        await session.execute(
            select(AnswerVersion.id, AnswerVersion.student_id, AnswerVersion.question_version_id)
            .where(AnswerVersion.id.in_(data.answer_version_ids))
            .order_by(AnswerVersion.id)
        )
    ).all()
    if len(answers) != len(data.answer_version_ids):
        raise DomainError("invalid_batch_scope", "Answer version does not match rubric question")
    seen_students: set[UUID] = set()
    for answer in answers:
        if answer.question_version_id != frozen.question_version_id:
            raise DomainError(
                "invalid_batch_scope", "Answer version does not match rubric question"
            )
        if answer.student_id in seen_students:
            raise DomainError(
                "duplicate_student", "Choose one answer version per student per batch"
            )
        seen_students.add(answer.student_id)
    batch = GradingBatch(teacher_id=teacher, rubric_version_id=frozen.id)
    session.add(batch)
    await session.flush()
    # All writers insert and lock shared targets in answer-ID order. A single
    # bounded insert still uses the database uniqueness rule across processes.
    await session.execute(
        insert(GradingItem)
        .values(
            [
                dict(
                    id=uuid4(),
                    teacher_id=teacher,
                    answer_version_id=answer.id,
                    rubric_version_id=frozen.id,
                    evaluation_profile=profile.id,
                    max_calls=len(rubric.criteria) * 6,
                )
                for answer in answers
            ]
        )
        .on_conflict_do_nothing(
            index_elements=["answer_version_id", "rubric_version_id", "evaluation_profile"]
        )
    )
    items = list(
        await session.scalars(
            select(GradingItem.id)
            .where(
                GradingItem.answer_version_id.in_(data.answer_version_ids),
                GradingItem.rubric_version_id == frozen.id,
                GradingItem.evaluation_profile == profile.id,
            )
            .order_by(GradingItem.answer_version_id)
            .with_for_update()
        )
    )
    session.add_all(BatchItem(batch_id=batch.id, item_id=item_id) for item_id in items)
    await session.flush()
    await refresh_batch(session, batch.id)
    audit(session, teacher, "grading_batch_created", batch.id)
    return {
        "id": str(batch.id),
        "item_ids": [str(item_id) for item_id in items],
        "evaluation_profile": profile.id,
    }


async def batch_view(session: AsyncSession, teacher: UUID, batch_id: UUID) -> dict:
    batch = await owned_batch(session, teacher, batch_id)
    frozen = await session.get(RubricVersion, batch.rubric_version_id)
    question = await session.get(QuestionVersion, frozen.question_version_id)
    assignment = await session.get(Assignment, question.assignment_id)
    items = (
        await session.execute(
            select(
                GradingItem.id,
                GradingItem.status,
                GradingItem.review_revision,
                GradingItem.last_error,
            )
            .join(BatchItem, BatchItem.item_id == GradingItem.id)
            .where(BatchItem.batch_id == batch.id)
            .order_by(GradingItem.created_at, GradingItem.id)
        )
    ).all()
    return {
        "id": str(batch.id),
        "created_at": json_time(batch.created_at),
        "rubric_version_id": str(batch.rubric_version_id),
        "rubric_version": frozen.version,
        "assignment": {
            "id": str(assignment.id),
            "title": assignment.title,
            "course_id": str(assignment.course_id),
            "classroom_id": str(assignment.classroom_id),
        },
        "question": {
            "id": str(question.id),
            "question_key": question.question_key,
            "version": question.version,
        },
        "status": batch.status,
        "outcome": batch.outcome,
        "counts": batch.counts,
        "items": [
            {
                "id": str(i.id),
                "status": i.status,
                "review_revision": i.review_revision,
                "review_status": "confirmed" if i.review_revision else "pending",
                "error_code": i.last_error,
            }
            for i in items
        ],
    }


async def item_view(session: AsyncSession, teacher: UUID, item_id: UUID) -> dict:
    return (await item_views(session, teacher, [item_id]))[item_id]


async def item_views(session: AsyncSession, teacher: UUID, item_ids: list[UUID]) -> dict:
    """Read a bounded set in the caller's snapshot, sharing fixed version records."""
    if not item_ids:
        return {}
    rows = (
        await session.execute(
            select(GradingItem, AnswerVersion, Student)
            .join(AnswerVersion, AnswerVersion.id == GradingItem.answer_version_id)
            .join(Student, Student.id == AnswerVersion.student_id)
            .where(GradingItem.id.in_(item_ids), GradingItem.teacher_id == teacher)
        )
    ).all()
    if len(rows) != len(set(item_ids)):
        raise DomainError("not_found", "Grading item not found", 404)

    async def records(model, ids):
        return {
            row.id: row
            for row in await session.scalars(select(model).where(model.id.in_(set(ids))))
        }

    profiles = await records(EvaluationProfile, (item.evaluation_profile for item, _, _ in rows))
    rubrics = await records(RubricVersion, (item.rubric_version_id for item, _, _ in rows))
    questions = await records(
        QuestionVersion, (rubric.question_version_id for rubric in rubrics.values())
    )
    assignments = await records(
        Assignment, (question.assignment_id for question in questions.values())
    )

    async def grouped(model, order):
        result = defaultdict(list)
        for row in await session.scalars(
            select(model).where(model.item_id.in_(item_ids)).order_by(model.item_id, *order)
        ):
            result[row.item_id].append(row)
        return result

    assessments = await grouped(Assessment, [Assessment.criterion_id])
    steps = {
        row.id: row for row in await session.scalars(select(Step).where(Step.item_id.in_(item_ids)))
    }
    reviews = await grouped(Review, [Review.revision])
    calls = await grouped(ModelCall, [ModelCall.created_at, ModelCall.id])
    result = {}
    for item, answer, student in rows:
        frozen = rubrics[item.rubric_version_id]
        question = questions[frozen.question_version_id]
        result[item.id] = _item_view(
            item,
            profiles[item.evaluation_profile],
            answer,
            frozen,
            question,
            student,
            assignments[question.assignment_id],
            assessments[item.id],
            steps,
            reviews[item.id],
            calls[item.id],
        )
    return result


def _item_view(
    item, profile, answer, frozen, question, student, assignment, assessments, steps, reviews, calls
):
    return {
        "id": str(item.id),
        "status": item.status,
        "error_code": item.last_error,
        "evaluation_profile": item.evaluation_profile,
        "evaluation_profile_manifest": profile.manifest,
        "simulation": item.evaluation_profile == "fake-v1",
        "student": {
            "id": str(student.id),
            "name": student.name,
            "external_id": student.external_id,
        },
        "assignment": {
            "id": str(assignment.id),
            "title": assignment.title,
            "course_id": str(assignment.course_id),
            "classroom_id": str(assignment.classroom_id),
        },
        "answer": {
            "id": str(answer.id),
            "text": answer.answer_text,
            "content_hash": answer.content_hash,
            "source_revision": answer.source_revision,
        },
        "question": {
            "id": str(question.id),
            "question_key": question.question_key,
            "version": question.version,
            "prompt": question.prompt,
            "reference": question.reference,
        },
        "rubric": {"id": str(frozen.id), "version": frozen.version, "body": frozen.body},
        "suggested_score": str(item.suggested_score) if item.suggested_score is not None else None,
        "review_revision": item.review_revision,
        "confirmed_score": str(reviews[-1].confirmed_score) if reviews else None,
        "assessments": [
            {
                "criterion_id": a.criterion_id,
                "candidate_level_id": a.level_id,
                "suggested_points": str(a.suggested_points)
                if a.suggested_points is not None
                else None,
                "evaluation": steps[a.evaluation_step_id].output,
                "evidence": a.evidence,
                "issues": a.issues,
                "evidence_support": "requires_teacher_review",
            }
            for a in assessments
        ],
        "reviews": [
            {
                "id": str(r.id),
                "revision": r.revision,
                "decisions": r.decisions,
                "confirmed_score": str(r.confirmed_score),
                "created_at": json_time(r.created_at),
            }
            for r in reviews
        ],
        "execution": {
            "epoch": item.epoch,
            "attempts": item.attempts,
            "calls_used": item.calls_used,
            "max_calls": item.max_calls,
            "calls": [
                {
                    "id": str(c.id),
                    "criterion_id": c.criterion_id,
                    "kind": c.kind,
                    "epoch": c.epoch,
                    "status": c.status,
                    "input_hash": c.input_hash,
                    "requested_model": c.requested_model,
                    "returned_model": c.returned_model,
                    "adapter_version": c.adapter_version,
                    "duration_ms": c.duration_ms,
                    "usage": c.usage,
                    "error_code": c.error_code,
                    "provider_request_id": c.provider_request_id,
                    "provider_fingerprint": c.provider_fingerprint,
                    "prompt_hash": c.prompt_hash,
                    "http_status": c.http_status,
                    "error_retryable": c.error_retryable,
                }
                for c in calls
            ],
        },
    }


async def review_item(
    session: AsyncSession, teacher: UUID, item_id: UUID, data: ReviewInput
) -> dict:
    item = await owned_item(session, teacher, item_id, lock=True)
    if item.status != "succeeded":
        raise DomainError("not_ready_for_review", "Wait for a complete assessment", 409)
    if item.review_revision != data.expected_revision:
        raise DomainError("revision_conflict", "Review changed; reload before submitting", 409)
    frozen = await session.get(RubricVersion, item.rubric_version_id)
    answer = await session.get(AnswerVersion, item.answer_version_id)
    rubric = Rubric.model_validate(frozen.body)
    selected = {d.criterion_id: d.level_id for d in data.decisions}
    score = calculate_score(rubric, selected)
    assessments = {
        a.criterion_id: a
        for a in await session.scalars(select(Assessment).where(Assessment.item_id == item.id))
    }
    normalized = []
    for decision in data.decisions:
        original = assessments[decision.criterion_id].evidence
        citations = (
            decision.citations
            if decision.citations is not None
            else [Evidence.model_validate(c) for c in original["citations"]]
        )
        if decision.evidence_support == "absence_confirmed":
            citations = []
        evidence = EvidenceOutput(
            citations=citations,
            absence_claim=decision.evidence_support == "absence_confirmed",
            extractor_version="teacher",
        )
        validate_evidence(answer.answer_text, evidence)
        if decision.evidence_support == "supported" and not citations:
            raise DomainError("missing_review_evidence", "Supported decisions require a citation")
        normalized.append(
            {
                **decision.model_dump(mode="json"),
                "citations": [c.model_dump() for c in citations],
                "answer_version_id": str(answer.id),
                "rubric_version_id": str(frozen.id),
            }
        )
    item.review_revision += 1
    review = Review(
        item_id=item.id,
        revision=item.review_revision,
        teacher_id=teacher,
        decisions=normalized,
        confirmed_score=score,
    )
    session.add(review)
    await session.flush()
    audit(
        session,
        teacher,
        "grade_reviewed",
        review.id,
        item_id=str(item.id),
        revision=review.revision,
    )
    await refresh_batches(session, item.id)
    return {"id": str(review.id), "revision": review.revision, "confirmed_score": str(score)}


async def retry_item(session: AsyncSession, teacher: UUID, item_id: UUID, data: RetryInput) -> dict:
    item = await owned_item(session, teacher, item_id, lock=True)
    if item.status != "failed":
        raise DomainError("not_retryable", "Only failed items can be explicitly retried", 409)
    item.max_attempts += data.additional_attempts
    item.max_calls += data.additional_calls
    item.status, item.last_error = "pending", None
    item.ready_at = await session.scalar(select(func.clock_timestamp()))
    audit(session, teacher, "grading_retry_requested", item.id, **data.model_dump(mode="json"))
    await session.flush()
    await refresh_batches(session, item.id)
    return {"id": str(item.id), "status": item.status, "max_calls": item.max_calls}


async def create_export(
    session: AsyncSession, teacher: UUID, batch_id: UUID, data: ExportInput
) -> dict:
    """Caller must provide a REPEATABLE READ transaction, including all views and counts."""
    batch = await owned_batch(session, teacher, batch_id)
    frozen = await session.get(RubricVersion, batch.rubric_version_id)
    question = await session.get(QuestionVersion, frozen.question_version_id)
    assignment = await session.get(Assignment, question.assignment_id)
    roster_size = await session.scalar(
        select(func.count())
        .select_from(AssignmentMember)
        .where(AssignmentMember.assignment_id == assignment.id)
    )
    selected = (
        await session.execute(
            select(GradingItem.id, GradingItem.teacher_id, GradingItem.review_revision)
            .join(BatchItem, BatchItem.item_id == GradingItem.id)
            .where(BatchItem.batch_id == batch.id)
            .order_by(BatchItem.item_id)
        )
    ).all()
    if any(row.teacher_id != teacher for row in selected):
        raise DomainError("not_found", "Grading item not found", 404)
    ids = [row.id for row in selected]
    confirmed = sum(row.review_revision > 0 for row in selected)
    included = [row.id for row in selected if data.mode == "working" or row.review_revision > 0]
    views = await item_views(session, teacher, included)
    rows = []
    for item_id in included:
        view = views[item_id]
        is_confirmed = view["review_revision"] > 0
        rows.append(
            {
                "student_id": view["student"]["id"],
                "external_student_id": view["student"]["external_id"],
                "student_name": view["student"]["name"],
                "review_status": "confirmed" if is_confirmed else "pending",
                **view,
            }
        )
    body: dict[str, Any] = {
        "schema_version": "grading-export-v1",
        "mode": data.mode,
        "snapshot_at": json_time(await session.scalar(select(func.transaction_timestamp()))),
        "batch_id": str(batch.id),
        "assignment_id": str(assignment.id),
        "question_version_id": str(question.id),
        "rubric_version_id": str(frozen.id),
        "rubric_hash": frozen.content_hash,
        "counts": {
            "assignment_roster": roster_size,
            "selected_answers": len(ids),
            "confirmed": confirmed,
            "not_confirmed": len(ids) - confirmed,
            "exported": len(rows),
            "excluded": len(ids) - len(rows),
            "failed": batch.counts.get("failed", 0),
        },
        "rows": rows,
    }
    exported = ExportSnapshot(
        batch_id=batch.id, teacher_id=teacher, body=body, content_hash=digest(body)
    )
    session.add(exported)
    await session.flush()
    audit(session, teacher, "export_created", exported.id, batch_id=str(batch.id), mode=data.mode)
    return {"id": str(exported.id), "content_hash": exported.content_hash, "snapshot": body}

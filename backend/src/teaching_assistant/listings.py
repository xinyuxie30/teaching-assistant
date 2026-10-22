"""Scoped read models and stable keyset pagination; no model calls or business writes."""

import base64
import binascii
import json
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from teaching_assistant.db import (
    AnswerVersion,
    Assessment,
    Assignment,
    AssignmentMember,
    BatchItem,
    Classroom,
    Course,
    ExportSnapshot,
    GradingBatch,
    GradingItem,
    ImportRecord,
    Membership,
    QuestionVersion,
    Review,
    RubricDraft,
    RubricVersion,
    Student,
)
from teaching_assistant.domain import DomainError, digest
from teaching_assistant.schemas import (
    AnswerQuery,
    AssignmentQuery,
    BatchQuery,
    DraftQuery,
    ExportQuery,
    ItemQuery,
    PageQuery,
    QuestionQuery,
    SearchQuery,
    StudentQuery,
)
from teaching_assistant.service import (
    owned_assignment,
    owned_batch,
    owned_course,
    owned_question,
    owned_rubric,
)


def json_value(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (UUID, Decimal)):
        return str(value)
    return value


def cursor_encode(created_at: datetime, record_id: UUID, context: str) -> str:
    value = {"v": 1, "at": created_at.isoformat(), "id": str(record_id), "context": context}
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


def cursor_decode(value: str, context: str) -> tuple[datetime, UUID]:
    try:
        payload = json.loads(
            base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        )
        if (
            not isinstance(payload, dict)
            or payload.get("v") != 1
            or payload.get("context") != context
        ):
            raise ValueError("Cursor does not belong to this query")
        if not isinstance(payload.get("at"), str) or not isinstance(payload.get("id"), str):
            raise ValueError("Invalid cursor boundary")
        at, record_id = datetime.fromisoformat(payload["at"]), UUID(payload["id"])
        if at.tzinfo is None:
            raise ValueError("Timezone missing")
        return at, record_id
    except (ValueError, TypeError, KeyError, binascii.Error) as exc:
        raise DomainError(
            "invalid_cursor", "Cursor is invalid or belongs to another filter"
        ) from exc


async def page(
    session: AsyncSession, query, model, teacher: UUID, scope: str, params: PageQuery
) -> dict:
    if params.created_from is not None:
        query = query.where(model.created_at >= params.created_from)
    if params.created_to is not None:
        query = query.where(model.created_at <= params.created_to)
    context = digest(
        {
            "teacher": str(teacher),
            "scope": scope,
            "filters": params.model_dump(mode="json", exclude={"cursor", "limit"}),
        }
    )
    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    if params.cursor:
        at, record_id = cursor_decode(params.cursor, context)
        query = query.where(
            or_(
                model.created_at < at,
                and_(model.created_at == at, model.id < record_id),
            )
        )
    rows = (
        (
            await session.execute(
                query.order_by(model.created_at.desc(), model.id.desc()).limit(params.limit + 1)
            )
        )
        .mappings()
        .all()
    )
    has_more = len(rows) > params.limit
    rows = rows[: params.limit]
    next_cursor = (
        cursor_encode(rows[-1]["created_at"], rows[-1]["id"], context) if has_more else None
    )
    return {
        "items": [{key: json_value(value) for key, value in row.items()} for row in rows],
        "total": total,
        "limit": params.limit,
        "has_more": has_more,
        "next_cursor": next_cursor,
    }


def basic_columns(model):
    return model.id, model.created_at


async def owned_classroom(session: AsyncSession, teacher: UUID, classroom_id: UUID) -> Classroom:
    value = await session.scalar(
        select(Classroom)
        .join(Course)
        .where(Classroom.id == classroom_id, Course.teacher_id == teacher)
    )
    if value is None:
        raise DomainError("not_found", "Classroom not found", 404)
    return value


async def owned_student(session: AsyncSession, teacher: UUID, student_id: UUID) -> Student:
    value = await session.scalar(
        select(Student).join(Course).where(Student.id == student_id, Course.teacher_id == teacher)
    )
    if value is None:
        raise DomainError("not_found", "Student not found", 404)
    return value


async def courses(session, teacher: UUID, params: SearchQuery) -> dict:
    query = select(*basic_columns(Course), Course.name).where(Course.teacher_id == teacher)
    if params.q:
        query = query.where(Course.name.icontains(params.q, autoescape=True))
    return await page(session, query, Course, teacher, "courses", params)


async def classrooms(session, teacher: UUID, course_id: UUID, params: SearchQuery) -> dict:
    await owned_course(session, teacher, course_id)
    query = select(*basic_columns(Classroom), Classroom.course_id, Classroom.name).where(
        Classroom.course_id == course_id
    )
    if params.q:
        query = query.where(Classroom.name.icontains(params.q, autoescape=True))
    return await page(session, query, Classroom, teacher, f"classrooms:{course_id}", params)


async def students(session, teacher: UUID, course_id: UUID, params: StudentQuery) -> dict:
    await owned_course(session, teacher, course_id)
    query = select(
        *basic_columns(Student), Student.course_id, Student.external_id, Student.name
    ).where(Student.course_id == course_id)
    if params.classroom_id:
        await owned_classroom(session, teacher, params.classroom_id)
        query = query.where(
            select(Membership.student_id)
            .where(
                Membership.student_id == Student.id,
                Membership.classroom_id == params.classroom_id,
            )
            .exists()
        )
    if params.q:
        query = query.where(
            or_(
                Student.name.icontains(params.q, autoescape=True),
                Student.external_id.icontains(params.q, autoescape=True),
            )
        )
    return await page(session, query, Student, teacher, f"students:{course_id}", params)


async def assignments(session, teacher: UUID, course_id: UUID, params: AssignmentQuery) -> dict:
    await owned_course(session, teacher, course_id)
    query = (
        select(
            *basic_columns(Assignment),
            Assignment.course_id,
            Assignment.classroom_id,
            Assignment.title,
            Classroom.name.label("classroom_name"),
        )
        .join(Classroom, Classroom.id == Assignment.classroom_id)
        .where(Assignment.course_id == course_id)
    )
    if params.classroom_id:
        await owned_classroom(session, teacher, params.classroom_id)
        query = query.where(Assignment.classroom_id == params.classroom_id)
    if params.q:
        query = query.where(Assignment.title.icontains(params.q, autoescape=True))
    return await page(session, query, Assignment, teacher, f"assignments:{course_id}", params)


async def questions(session, teacher: UUID, assignment_id: UUID, params: QuestionQuery) -> dict:
    await owned_assignment(session, teacher, assignment_id)
    query = select(
        *basic_columns(QuestionVersion),
        QuestionVersion.assignment_id,
        QuestionVersion.question_key,
        QuestionVersion.version,
        QuestionVersion.maximum_points,
        QuestionVersion.content_hash,
        func.left(QuestionVersion.prompt, 240).label("prompt_preview"),
    ).where(QuestionVersion.assignment_id == assignment_id)
    if params.question_key:
        query = query.where(QuestionVersion.question_key == params.question_key)
    return await page(
        session, query, QuestionVersion, teacher, f"questions:{assignment_id}", params
    )


async def drafts(session, teacher: UUID, question_id: UUID, params: DraftQuery) -> dict:
    await owned_question(session, teacher, question_id)
    query = select(
        *basic_columns(RubricDraft),
        RubricDraft.question_version_id,
        RubricDraft.revision,
        RubricDraft.confirmed_version_id,
        case((RubricDraft.confirmed_version_id.is_(None), "draft"), else_="confirmed").label(
            "status"
        ),
    ).where(RubricDraft.question_version_id == question_id)
    if params.status == "draft":
        query = query.where(RubricDraft.confirmed_version_id.is_(None))
    elif params.status == "confirmed":
        query = query.where(RubricDraft.confirmed_version_id.is_not(None))
    return await page(session, query, RubricDraft, teacher, f"drafts:{question_id}", params)


async def rubrics(session, teacher: UUID, question_id: UUID, params: PageQuery) -> dict:
    await owned_question(session, teacher, question_id)
    query = select(
        *basic_columns(RubricVersion),
        RubricVersion.question_version_id,
        RubricVersion.version,
        RubricVersion.content_hash,
        RubricVersion.confirmed_by,
        func.jsonb_array_length(RubricVersion.body["criteria"]).label("criterion_count"),
    ).where(RubricVersion.question_version_id == question_id)
    return await page(session, query, RubricVersion, teacher, f"rubrics:{question_id}", params)


async def answers(session, teacher: UUID, assignment_id: UUID, params: AnswerQuery) -> dict:
    await owned_assignment(session, teacher, assignment_id)
    query = (
        select(
            *basic_columns(AnswerVersion),
            AnswerVersion.question_version_id,
            AnswerVersion.student_id,
            AnswerVersion.external_submission_id,
            AnswerVersion.source_revision,
            AnswerVersion.submitted_at,
            AnswerVersion.content_hash,
            func.left(AnswerVersion.answer_text, 240).label("answer_preview"),
            Student.name.label("student_name"),
            Student.external_id.label("external_student_id"),
            QuestionVersion.question_key,
            QuestionVersion.version.label("question_version"),
        )
        .join(QuestionVersion, QuestionVersion.id == AnswerVersion.question_version_id)
        .join(Student, Student.id == AnswerVersion.student_id)
        .where(QuestionVersion.assignment_id == assignment_id)
    )
    if params.question_version_id:
        await owned_question(session, teacher, params.question_version_id)
        query = query.where(AnswerVersion.question_version_id == params.question_version_id)
    if params.student_id:
        await owned_student(session, teacher, params.student_id)
        query = query.where(AnswerVersion.student_id == params.student_id)
    if params.source_revision:
        query = query.where(AnswerVersion.source_revision == params.source_revision)
    return await page(session, query, AnswerVersion, teacher, f"answers:{assignment_id}", params)


async def imports(session, teacher: UUID, assignment_id: UUID, params: PageQuery) -> dict:
    await owned_assignment(session, teacher, assignment_id)
    query = select(
        *basic_columns(ImportRecord),
        ImportRecord.assignment_id,
        ImportRecord.request_hash,
        func.jsonb_array_length(ImportRecord.answer_version_ids).label("answer_count"),
    ).where(ImportRecord.assignment_id == assignment_id, ImportRecord.teacher_id == teacher)
    return await page(session, query, ImportRecord, teacher, f"imports:{assignment_id}", params)


async def grading_batches(session, teacher: UUID, params: BatchQuery) -> dict:
    query = (
        select(
            *basic_columns(GradingBatch),
            GradingBatch.rubric_version_id,
            GradingBatch.status,
            GradingBatch.outcome,
            GradingBatch.counts,
            QuestionVersion.id.label("question_version_id"),
            QuestionVersion.assignment_id,
            Assignment.course_id,
            Assignment.title.label("assignment_title"),
        )
        .join(RubricVersion, RubricVersion.id == GradingBatch.rubric_version_id)
        .join(QuestionVersion, QuestionVersion.id == RubricVersion.question_version_id)
        .join(Assignment, Assignment.id == QuestionVersion.assignment_id)
        .where(GradingBatch.teacher_id == teacher)
    )
    if params.course_id:
        await owned_course(session, teacher, params.course_id)
        query = query.where(Assignment.course_id == params.course_id)
    if params.assignment_id:
        await owned_assignment(session, teacher, params.assignment_id)
        query = query.where(Assignment.id == params.assignment_id)
    if params.rubric_version_id:
        await owned_rubric(session, teacher, params.rubric_version_id)
        query = query.where(GradingBatch.rubric_version_id == params.rubric_version_id)
    if params.status:
        query = query.where(GradingBatch.status == params.status)
    if params.outcome:
        query = query.where(GradingBatch.outcome == params.outcome)
    return await page(session, query, GradingBatch, teacher, "grading-batches", params)


async def grading_items(session, teacher: UUID, params: ItemQuery) -> dict:
    query = (
        select(
            *basic_columns(GradingItem),
            GradingItem.answer_version_id,
            GradingItem.rubric_version_id,
            GradingItem.evaluation_profile,
            GradingItem.status,
            GradingItem.last_error.label("error_code"),
            GradingItem.suggested_score,
            GradingItem.review_revision,
            Review.confirmed_score,
            (GradingItem.evaluation_profile == "fake-v1").label("simulation"),
            case((GradingItem.review_revision > 0, "confirmed"), else_="pending").label(
                "review_status"
            ),
            and_(GradingItem.status == "succeeded", GradingItem.review_revision == 0).label(
                "needs_review"
            ),
            AnswerVersion.student_id,
            Student.name.label("student_name"),
            Student.external_id.label("external_student_id"),
            AnswerVersion.question_version_id,
            QuestionVersion.assignment_id,
            Assignment.course_id,
        )
        .join(AnswerVersion, AnswerVersion.id == GradingItem.answer_version_id)
        .join(Student, Student.id == AnswerVersion.student_id)
        .join(QuestionVersion, QuestionVersion.id == AnswerVersion.question_version_id)
        .join(Assignment, Assignment.id == QuestionVersion.assignment_id)
        .outerjoin(
            Review,
            and_(Review.item_id == GradingItem.id, Review.revision == GradingItem.review_revision),
        )
        .where(GradingItem.teacher_id == teacher)
    )
    if params.course_id:
        await owned_course(session, teacher, params.course_id)
        query = query.where(Assignment.course_id == params.course_id)
    if params.assignment_id:
        await owned_assignment(session, teacher, params.assignment_id)
        query = query.where(Assignment.id == params.assignment_id)
    if params.batch_id:
        await owned_batch(session, teacher, params.batch_id)
        query = query.where(
            select(BatchItem.item_id)
            .where(BatchItem.item_id == GradingItem.id, BatchItem.batch_id == params.batch_id)
            .exists()
        )
    if params.rubric_version_id:
        await owned_rubric(session, teacher, params.rubric_version_id)
        query = query.where(GradingItem.rubric_version_id == params.rubric_version_id)
    if params.student_id:
        await owned_student(session, teacher, params.student_id)
        query = query.where(AnswerVersion.student_id == params.student_id)
    if params.status:
        query = query.where(GradingItem.status == params.status)
    if params.review_status:
        query = query.where(
            GradingItem.review_revision == 0
            if params.review_status == "pending"
            else GradingItem.review_revision > 0
        )
    if params.evaluation_profile:
        query = query.where(GradingItem.evaluation_profile == params.evaluation_profile)
    if params.issue_code:
        query = query.where(
            select(Assessment.id)
            .where(
                Assessment.item_id == GradingItem.id,
                Assessment.issues.contains([params.issue_code]),
            )
            .exists()
        )
    return await page(session, query, GradingItem, teacher, "grading-items", params)


async def exports(session, teacher: UUID, params: ExportQuery) -> dict:
    # Do not load all answer texts and evidence from every stored export just to list its header.
    query = select(
        *basic_columns(ExportSnapshot),
        ExportSnapshot.batch_id,
        ExportSnapshot.content_hash,
        ExportSnapshot.body["mode"].as_string().label("mode"),
        ExportSnapshot.body["counts"].label("counts"),
    ).where(ExportSnapshot.teacher_id == teacher)
    if params.batch_id:
        await owned_batch(session, teacher, params.batch_id)
        query = query.where(ExportSnapshot.batch_id == params.batch_id)
    if params.mode:
        query = query.where(ExportSnapshot.body["mode"].as_string() == params.mode)
    return await page(session, query, ExportSnapshot, teacher, "exports", params)


async def assignment_detail(session, teacher: UUID, assignment_id: UUID) -> dict:
    assignment = await owned_assignment(session, teacher, assignment_id)
    course = await session.get(Course, assignment.course_id)
    classroom = await session.get(Classroom, assignment.classroom_id)
    # Import authorization uses this frozen roster, not today's classroom membership.
    rows = (
        (
            await session.execute(
                select(Student.id, Student.external_id, Student.name)
                .join(AssignmentMember, AssignmentMember.student_id == Student.id)
                .where(AssignmentMember.assignment_id == assignment.id)
                .order_by(Student.external_id, Student.id)
            )
        )
        .mappings()
        .all()
    )
    return {
        "id": str(assignment.id),
        "created_at": json_value(assignment.created_at),
        "title": assignment.title,
        "course_id": str(course.id),
        "course_name": course.name,
        "classroom_id": str(classroom.id),
        "classroom_name": classroom.name,
        "roster": [{key: json_value(value) for key, value in row.items()} for row in rows],
    }


async def question_detail(session, teacher: UUID, question_id: UUID) -> dict:
    value = await owned_question(session, teacher, question_id)
    return {
        key: json_value(getattr(value, key))
        for key in (
            "id",
            "created_at",
            "assignment_id",
            "question_key",
            "version",
            "prompt",
            "reference",
            "maximum_points",
            "content_hash",
        )
    }


async def rubric_detail(session, teacher: UUID, rubric_id: UUID) -> dict:
    value = await owned_rubric(session, teacher, rubric_id)
    return {
        key: json_value(getattr(value, key))
        for key in (
            "id",
            "created_at",
            "question_version_id",
            "version",
            "body",
            "content_hash",
            "confirmed_by",
        )
    }


async def draft_detail(session, teacher: UUID, draft_id: UUID) -> dict:
    value = await session.get(RubricDraft, draft_id)
    if value is None:
        raise DomainError("not_found", "Draft not found", 404)
    await owned_question(session, teacher, value.question_version_id)
    return {
        key: json_value(getattr(value, key))
        for key in (
            "id",
            "created_at",
            "question_version_id",
            "revision",
            "body",
            "confirmed_version_id",
        )
    }


async def answer_detail(session, teacher: UUID, answer_id: UUID) -> dict:
    value = await session.get(AnswerVersion, answer_id)
    if value is None:
        raise DomainError("not_found", "Answer version not found", 404)
    await owned_question(session, teacher, value.question_version_id)
    return {
        key: json_value(getattr(value, key))
        for key in (
            "id",
            "created_at",
            "question_version_id",
            "student_id",
            "external_submission_id",
            "source_revision",
            "answer_text",
            "content_hash",
            "submitted_at",
        )
    }

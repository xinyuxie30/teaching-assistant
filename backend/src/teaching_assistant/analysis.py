"""Batch-scoped learning analysis from current teacher reviews, never model suggestions.

Callers use one REPEATABLE READ transaction for scope, counts and source revisions.
Batch creation requires one answer per student; analysis never selects a different
submission or falls back to a review of an older answer. One rubric defines the scope.
"""

from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

from sqlalchemy import and_, case, column, func, select, true
from sqlalchemy.dialects.postgresql import JSONB

from teaching_assistant.db import (
    AnswerVersion,
    Assignment,
    AssignmentMember,
    BatchItem,
    Classroom,
    EvaluationProfile,
    GradingItem,
    QuestionVersion,
    Review,
    RubricVersion,
    Student,
)
from teaching_assistant.domain import DomainError, Rubric, digest
from teaching_assistant.listings import page
from teaching_assistant.schemas import AnalysisStudentQuery
from teaching_assistant.service import owned_batch


def decimal_text(value: Decimal | None) -> str | None:
    return (
        str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) if value is not None else None
    )


def rate(numerator, denominator) -> str | None:
    return decimal_text(Decimal(numerator) * 100 / Decimal(denominator)) if denominator else None


async def sources(session, teacher: UUID, batch_id: UUID):
    batch = await owned_batch(session, teacher, batch_id)
    frozen = await session.get(RubricVersion, batch.rubric_version_id)
    raw = (
        select(
            GradingItem.id,
            GradingItem.created_at,
            GradingItem.status,
            GradingItem.answer_version_id,
            GradingItem.review_revision,
            GradingItem.evaluation_profile,
            AnswerVersion.student_id,
            AnswerVersion.source_revision,
            AnswerVersion.created_at.label("answer_imported_at"),
            Review.id.label("review_id"),
            Review.created_at.label("reviewed_at"),
            Review.confirmed_score,
            Review.decisions,
            func.coalesce(EvaluationProfile.manifest["simulation"].as_boolean(), False).label(
                "simulation"
            ),
        )
        .join(BatchItem, BatchItem.item_id == GradingItem.id)
        .join(AnswerVersion, AnswerVersion.id == GradingItem.answer_version_id)
        .join(EvaluationProfile, EvaluationProfile.id == GradingItem.evaluation_profile)
        .outerjoin(
            Review,
            and_(
                Review.item_id == GradingItem.id,
                Review.revision == GradingItem.review_revision,
                Review.teacher_id == teacher,
            ),
        )
        .where(
            BatchItem.batch_id == batch.id,
            GradingItem.teacher_id == teacher,
            GradingItem.rubric_version_id == frozen.id,
        )
        .cte("analysis_sources")
    )
    rows = select(
        raw,
        case(
            (and_(raw.c.status == "succeeded", raw.c.review_id.is_not(None)), "confirmed"),
            (raw.c.status == "succeeded", "pending_review"),
            (raw.c.status == "failed", "failed"),
            else_="in_progress",
        ).label("selection_status"),
    ).cte("analysis_rows")
    revisions = (
        await session.execute(
            select(rows.c.id, rows.c.status, rows.c.review_revision).order_by(rows.c.id)
        )
    ).all()
    basis = digest(
        {
            "batch_id": str(batch.id),
            "rubric_hash": frozen.content_hash,
            "items": [[str(item_id), status, revision] for item_id, status, revision in revisions],
        }
    )
    return frozen, rows, basis


def decisions_table(rows):
    return (
        func.jsonb_array_elements(rows.c.decisions)
        .table_valued(column("value", JSONB))
        .lateral("analysis_decisions")
    )


async def batch_analysis(session, teacher: UUID, batch_id: UUID) -> dict:
    frozen, rows, basis = await sources(session, teacher, batch_id)
    question = await session.get(QuestionVersion, frozen.question_version_id)
    assignment = await session.get(Assignment, question.assignment_id)
    classroom = await session.get(Classroom, assignment.classroom_id)
    rubric = Rubric.model_validate(frozen.body)
    confirmed = rows.c.selection_status == "confirmed"
    count_student = func.count(func.distinct(rows.c.student_id))
    totals = (
        (
            await session.execute(
                select(
                    func.count().label("batch_answers"),
                    count_student.label("batch_students"),
                    *[
                        count_student.filter(rows.c.selection_status == status).label(status)
                        for status in (
                            "confirmed",
                            "pending_review",
                            "failed",
                            "in_progress",
                        )
                    ],
                    func.count()
                    .filter(and_(confirmed, rows.c.simulation))
                    .label("simulated_confirmed"),
                    func.avg(rows.c.confirmed_score).filter(confirmed).label("average_score"),
                    func.min(rows.c.confirmed_score).filter(confirmed).label("minimum_score"),
                    func.max(rows.c.confirmed_score).filter(confirmed).label("maximum_score"),
                    func.min(rows.c.reviewed_at).filter(confirmed).label("reviewed_from"),
                    func.max(rows.c.reviewed_at).filter(confirmed).label("reviewed_to"),
                ).select_from(rows)
            )
        )
        .mappings()
        .one()
    )
    roster = await session.scalar(
        select(func.count())
        .select_from(AssignmentMember)
        .where(AssignmentMember.assignment_id == assignment.id)
    )
    # Buckets describe score ranges, not a pass threshold or a mastery judgement.
    bands = [("below_60", 0, 60), ("60_to_80", 60, 80), ("80_to_100", 80, 100), ("full", 100, None)]
    score100 = rows.c.confirmed_score * 100
    maximum = rubric.maximum_points
    distribution = (
        (
            await session.execute(
                select(
                    *[
                        func.count()
                        .filter(
                            and_(
                                confirmed,
                                score100 >= maximum * lower,
                                score100 < maximum * upper
                                if upper is not None
                                else score100 == maximum * 100,
                            )
                        )
                        .label(key)
                        for key, lower, upper in bands
                    ]
                ).select_from(rows)
            )
        )
        .mappings()
        .one()
    )
    decision = decisions_table(rows)
    criterion_id = decision.c.value["criterion_id"].astext
    level_id = decision.c.value["level_id"].astext
    level_counts = {
        (cid, lid): count
        for cid, lid, count in await session.execute(
            select(criterion_id, level_id, func.count())
            .select_from(rows.join(decision, true()))
            .where(confirmed)
            .group_by(criterion_id, level_id)
        )
    }
    criteria = []
    for criterion in rubric.criteria:
        levels = [
            {
                **level.model_dump(mode="json"),
                "students": level_counts.get((criterion.id, level.id), 0),
            }
            for level in sorted(criterion.levels, key=lambda level: level.ordinal)
        ]
        sample = sum(level["students"] for level in levels)
        earned = sum((Decimal(level["points"]) * level["students"] for level in levels), Decimal(0))
        below_full = sum(
            level["students"]
            for level in levels
            if Decimal(level["points"]) < criterion.maximum_points
        )
        criteria.append(
            {
                "id": criterion.id,
                "description": criterion.description,
                "maximum_points": decimal_text(criterion.maximum_points),
                "confirmed_students": sample,
                "average_points": decimal_text(earned / sample) if sample else None,
                "score_rate": rate(earned, sample * criterion.maximum_points),
                "below_full_credit_students": below_full,
                "below_full_credit_rate": rate(below_full, sample),
                "levels": levels,
            }
        )
    # Stable ties retain the teacher's rubric ordering.
    criteria.sort(key=lambda criterion: -criterion["below_full_credit_students"])
    return {
        "schema_version": "batch-analysis-v1",
        "snapshot_at": (await session.scalar(select(func.transaction_timestamp()))).isoformat(),
        "basis_hash": basis,
        "batch_id": str(batch_id),
        "scope": {
            "assignment_id": str(assignment.id),
            "assignment_title": assignment.title,
            "classroom_id": str(classroom.id),
            "classroom_name": classroom.name,
            "question_version_id": str(question.id),
            "question_key": question.question_key,
            "question_version": question.version,
            "rubric_version_id": str(frozen.id),
            "rubric_version": frozen.version,
            "rubric_hash": frozen.content_hash,
            "maximum_points": decimal_text(maximum),
            "selection_policy": "single-answer-current-confirmed-review-v1",
        },
        "coverage": {
            "assignment_roster": roster,
            **{
                key: totals[key]
                for key in (
                    "batch_answers",
                    "batch_students",
                    "confirmed",
                    "pending_review",
                    "failed",
                    "in_progress",
                    "simulated_confirmed",
                )
            },
            "not_in_batch": roster - totals["batch_students"],
            "confirmed_rate": rate(totals["confirmed"], totals["batch_students"]),
        },
        "scores": {
            **{
                key: decimal_text(totals[key])
                for key in ("average_score", "minimum_score", "maximum_score")
            },
            "reviewed_from": totals["reviewed_from"].isoformat()
            if totals["reviewed_from"]
            else None,
            "reviewed_to": totals["reviewed_to"].isoformat() if totals["reviewed_to"] else None,
            "distribution": [{"id": key, "students": distribution[key]} for key, _, _ in bands],
        },
        "criteria": criteria,
    }


async def analysis_students(
    session, teacher: UUID, batch_id: UUID, params: AnalysisStudentQuery
) -> dict:
    frozen, rows, basis = await sources(session, teacher, batch_id)
    if params.basis_hash and params.basis_hash != basis:
        raise DomainError(
            "analysis_changed",
            "The source reviews or task states changed; refresh the analysis",
            409,
        )
    query = select(
        rows.c.id,
        rows.c.created_at,
        rows.c.student_id,
        Student.name.label("student_name"),
        Student.external_id.label("external_student_id"),
        rows.c.answer_version_id,
        rows.c.source_revision,
        rows.c.status,
        rows.c.selection_status,
        rows.c.review_id,
        rows.c.review_revision,
        rows.c.reviewed_at,
        rows.c.confirmed_score,
        rows.c.simulation,
    ).join(Student, Student.id == rows.c.student_id)
    if params.status:
        query = query.where(rows.c.selection_status == params.status)
    if params.criterion_id:
        rubric = Rubric.model_validate(frozen.body)
        criterion = next((c for c in rubric.criteria if c.id == params.criterion_id), None)
        if criterion is None:
            raise DomainError(
                "unknown_criterion", "Criterion does not belong to this batch's rubric"
            )
        decision = decisions_table(rows)
        query = query.join(decision, true()).where(
            rows.c.selection_status == "confirmed",
            decision.c.value["criterion_id"].astext == criterion.id,
        )
        if params.below_full_credit:
            query = query.where(
                decision.c.value["level_id"].astext.in_(
                    [
                        level.id
                        for level in criterion.levels
                        if level.points < criterion.maximum_points
                    ]
                )
            )
        query = query.add_columns(
            decision.c.value["level_id"].astext.label("level_id"),
            decision.c.value["reason"].astext.label("review_reason"),
        )
    result = await page(session, query, rows.c, teacher, f"analysis:{batch_id}", params)
    return {**result, "basis_hash": basis}

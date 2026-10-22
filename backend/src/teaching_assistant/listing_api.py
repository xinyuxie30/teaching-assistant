from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from teaching_assistant import analysis, listings
from teaching_assistant.db import Database
from teaching_assistant.schemas import (
    AnalysisStudentQuery,
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


def create_listing_router(db: Database, identity) -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["lists"])
    TeacherID = Annotated[UUID, Depends(identity)]

    async def read(handler, *args):
        # Page rows and total come from the same database snapshot.
        async with db.snapshot_sessions.begin() as session:
            return await handler(session, *args)

    @router.get("/grading-batches/{batch_id}/analysis")
    async def batch_analysis(batch_id: UUID, teacher: TeacherID):
        return await read(analysis.batch_analysis, teacher, batch_id)

    @router.get("/grading-batches/{batch_id}/analysis/students")
    async def analysis_students(
        batch_id: UUID, teacher: TeacherID, params: Annotated[AnalysisStudentQuery, Query()]
    ):
        return await read(analysis.analysis_students, teacher, batch_id, params)

    @router.get("/courses")
    async def courses(teacher: TeacherID, params: Annotated[SearchQuery, Query()]):
        return await read(listings.courses, teacher, params)

    @router.get("/courses/{course_id}/classrooms")
    async def classrooms(
        course_id: UUID, teacher: TeacherID, params: Annotated[SearchQuery, Query()]
    ):
        return await read(listings.classrooms, teacher, course_id, params)

    @router.get("/courses/{course_id}/students")
    async def students(
        course_id: UUID, teacher: TeacherID, params: Annotated[StudentQuery, Query()]
    ):
        return await read(listings.students, teacher, course_id, params)

    @router.get("/courses/{course_id}/assignments")
    async def assignments(
        course_id: UUID, teacher: TeacherID, params: Annotated[AssignmentQuery, Query()]
    ):
        return await read(listings.assignments, teacher, course_id, params)

    @router.get("/assignments/{assignment_id}/question-versions")
    async def questions(
        assignment_id: UUID, teacher: TeacherID, params: Annotated[QuestionQuery, Query()]
    ):
        return await read(listings.questions, teacher, assignment_id, params)

    @router.get("/assignments/{assignment_id}")
    async def assignment_detail(assignment_id: UUID, teacher: TeacherID):
        return await read(listings.assignment_detail, teacher, assignment_id)

    @router.get("/question-versions/{question_id}/rubric-drafts")
    async def drafts(question_id: UUID, teacher: TeacherID, params: Annotated[DraftQuery, Query()]):
        return await read(listings.drafts, teacher, question_id, params)

    @router.get("/question-versions/{question_id}/rubric-versions")
    async def rubrics(question_id: UUID, teacher: TeacherID, params: Annotated[PageQuery, Query()]):
        return await read(listings.rubrics, teacher, question_id, params)

    @router.get("/assignments/{assignment_id}/answer-versions")
    async def answers(
        assignment_id: UUID, teacher: TeacherID, params: Annotated[AnswerQuery, Query()]
    ):
        return await read(listings.answers, teacher, assignment_id, params)

    @router.get("/assignments/{assignment_id}/imports")
    async def imports(
        assignment_id: UUID, teacher: TeacherID, params: Annotated[PageQuery, Query()]
    ):
        return await read(listings.imports, teacher, assignment_id, params)

    @router.get("/grading-batches")
    async def batches(teacher: TeacherID, params: Annotated[BatchQuery, Query()]):
        return await read(listings.grading_batches, teacher, params)

    @router.get("/grading-items")
    async def items(teacher: TeacherID, params: Annotated[ItemQuery, Query()]):
        return await read(listings.grading_items, teacher, params)

    @router.get("/exports")
    async def exports(teacher: TeacherID, params: Annotated[ExportQuery, Query()]):
        return await read(listings.exports, teacher, params)

    @router.get("/question-versions/{question_id}")
    async def question_detail(question_id: UUID, teacher: TeacherID):
        return await read(listings.question_detail, teacher, question_id)

    @router.get("/rubric-versions/{rubric_id}")
    async def rubric_detail(rubric_id: UUID, teacher: TeacherID):
        return await read(listings.rubric_detail, teacher, rubric_id)

    @router.get("/rubric-drafts/{draft_id}")
    async def draft_detail(draft_id: UUID, teacher: TeacherID):
        return await read(listings.draft_detail, teacher, draft_id)

    @router.get("/answer-versions/{answer_id}")
    async def answer_detail(answer_id: UUID, teacher: TeacherID):
        return await read(listings.answer_detail, teacher, answer_id)

    return router

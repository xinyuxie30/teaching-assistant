import csv
import io
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, text

from teaching_assistant import service
from teaching_assistant.agent.api import router as agent_router
from teaching_assistant.db import Database, ExportSnapshot, Teacher
from teaching_assistant.domain import DomainError, text_digest
from teaching_assistant.execution_trace import item_trace
from teaching_assistant.listing_api import create_listing_router
from teaching_assistant.profiles import ProfileRegistry
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
)
from teaching_assistant.settings import Settings

security = HTTPBearer(auto_error=False)


def create_app(settings: Settings | None = None, database: Database | None = None) -> FastAPI:
    config = settings or Settings()
    db = database or Database(config.database_url)
    profiles = ProfileRegistry(config)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        if database is None:
            await db.close()

    app = FastAPI(
        title="教学助手 · 最小批改后端",
        description="支持模拟、Jev 与 DeepSeek 评分配置；所有结果均需老师复核。",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.db = db

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError):
        return JSONResponse(
            status_code=exc.status, content={"code": exc.code, "detail": exc.detail}
        )

    async def identity(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)],
    ) -> UUID:
        if credentials is None:
            raise DomainError("unauthorized", "Bearer token required", 401)
        async with db.sessions() as session:
            teacher = await session.scalar(
                select(Teacher.id).where(
                    Teacher.token_digest == text_digest(credentials.credentials)
                )
            )
        if teacher is None:
            raise DomainError("unauthorized", "Unknown token", 401)
        return teacher

    TeacherID = Annotated[UUID, Depends(identity)]
    IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=120)]
    app.include_router(create_listing_router(db, identity))
    app.include_router(agent_router(db, identity, config))

    async def write(teacher, key, scope, data, action, *, snapshot=False):
        return await service.write_operation(
            db, teacher, scope, key, data.model_dump(mode="json"), action, snapshot=snapshot
        )

    @app.get("/health")
    async def health():
        async with db.sessions() as session:
            await session.execute(text("SELECT 1"))
        return {"status": "ok", "evaluator": config.evaluator}

    @app.get("/v1/me")
    async def me(teacher: TeacherID):
        return {"teacher_id": str(teacher), "identity_mode": "local-development-token"}

    @app.get("/v1/evaluation-profiles")
    async def evaluation_profiles(teacher: TeacherID):
        return profiles.public()

    @app.post("/v1/workspaces", status_code=201)
    async def workspace(data: WorkspaceInput, teacher: TeacherID, key: IdempotencyKey):
        return await write(
            teacher, key, "workspace", data, lambda s: service.create_workspace(s, teacher, data)
        )

    @app.post("/v1/courses/{course_id}/assignments", status_code=201)
    async def assignment(
        course_id: UUID, data: AssignmentInput, teacher: TeacherID, key: IdempotencyKey
    ):
        return await write(
            teacher,
            key,
            f"assignments:{course_id}",
            data,
            lambda s: service.create_assignment(s, teacher, course_id, data),
        )

    @app.post("/v1/assignments/{assignment_id}/question-versions", status_code=201)
    async def question(
        assignment_id: UUID, data: QuestionInput, teacher: TeacherID, key: IdempotencyKey
    ):
        return await write(
            teacher,
            key,
            f"questions:{assignment_id}",
            data,
            lambda s: service.create_question(s, teacher, assignment_id, data),
        )

    @app.post("/v1/question-versions/{question_id}/rubric-drafts", status_code=201)
    async def draft(question_id: UUID, data: DraftInput, teacher: TeacherID, key: IdempotencyKey):
        return await write(
            teacher,
            key,
            f"drafts:{question_id}",
            data,
            lambda s: service.create_draft(s, teacher, question_id, data),
        )

    @app.post("/v1/rubric-drafts/{draft_id}/confirm")
    async def confirm(draft_id: UUID, data: ConfirmInput, teacher: TeacherID, key: IdempotencyKey):
        return await write(
            teacher,
            key,
            f"confirm:{draft_id}",
            data,
            lambda s: service.confirm_draft(s, teacher, draft_id, data),
        )

    @app.post("/v1/assignments/{assignment_id}/imports/preview")
    async def preview(assignment_id: UUID, data: ImportInput, teacher: TeacherID):
        async with db.sessions() as session:
            existing = await service.validate_import(session, teacher, assignment_id, data)
        return {
            "valid": True,
            "row_count": len(data.rows),
            "reused": sum(row is not None for row in existing),
            "notice": "Commit validates the complete payload again; preview reserves no records.",
        }

    @app.post("/v1/assignments/{assignment_id}/imports", status_code=201)
    async def import_answers(
        assignment_id: UUID, data: ImportInput, teacher: TeacherID, key: IdempotencyKey
    ):
        return await write(
            teacher,
            key,
            f"imports:{assignment_id}",
            data,
            lambda s: service.import_answers(s, teacher, assignment_id, data),
        )

    @app.post("/v1/grading-batches", status_code=202)
    async def batch(data: BatchInput, teacher: TeacherID, key: IdempotencyKey):
        return await write(
            teacher,
            key,
            "grading-batches",
            data,
            lambda s: service.create_batch(
                s, teacher, data, profile=profiles.resolve(data.evaluation_profile)
            ),
        )

    @app.get("/v1/grading-batches/{batch_id}")
    async def get_batch(batch_id: UUID, teacher: TeacherID):
        async with db.snapshot_sessions.begin() as session:
            return await service.batch_view(session, teacher, batch_id)

    @app.get("/v1/grading-items/{item_id}")
    async def get_item(item_id: UUID, teacher: TeacherID):
        async with db.snapshot_sessions.begin() as session:
            return await service.item_view(session, teacher, item_id)

    @app.get("/v1/grading-items/{item_id}/trace")
    async def get_item_trace(item_id: UUID, teacher: TeacherID):
        async with db.snapshot_sessions.begin() as session:
            return await item_trace(session, teacher, item_id)

    @app.post("/v1/grading-items/{item_id}/reviews")
    async def review(item_id: UUID, data: ReviewInput, teacher: TeacherID, key: IdempotencyKey):
        return await write(
            teacher,
            key,
            f"review:{item_id}",
            data,
            lambda s: service.review_item(s, teacher, item_id, data),
        )

    @app.post("/v1/grading-items/{item_id}/retry", status_code=202)
    async def retry(item_id: UUID, data: RetryInput, teacher: TeacherID, key: IdempotencyKey):
        return await write(
            teacher,
            key,
            f"retry:{item_id}",
            data,
            lambda s: service.retry_item(s, teacher, item_id, data),
        )

    @app.post("/v1/grading-batches/{batch_id}/exports", status_code=201)
    async def export(batch_id: UUID, data: ExportInput, teacher: TeacherID, key: IdempotencyKey):
        return await write(
            teacher,
            key,
            f"export:{batch_id}",
            data,
            lambda s: service.create_export(s, teacher, batch_id, data),
            snapshot=True,
        )

    @app.get("/v1/exports/{export_id}")
    async def download(
        export_id: UUID,
        teacher: TeacherID,
        format: Annotated[str, Query(pattern="^(json|csv)$")] = "json",
    ):
        async with db.sessions() as session:
            saved = await session.scalar(
                select(ExportSnapshot).where(
                    ExportSnapshot.id == export_id, ExportSnapshot.teacher_id == teacher
                )
            )
            if saved is None:
                raise DomainError("not_found", "Export not found", 404)
            body = saved.body
        if format == "json":
            return {"id": str(saved.id), "content_hash": saved.content_hash, "snapshot": body}
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(
            [
                "student_id",
                "external_student_id",
                "student_name",
                "execution_status",
                "review_status",
                "suggested_score",
                "confirmed_score",
                "review_revision",
                "answer_version_id",
                "rubric_version_id",
                "evaluation_profile",
                "simulation",
            ]
        )
        for row in body["rows"]:

            def safe(value):
                rendered = "" if value is None else str(value)
                # CSV is a presentation export; the exact original remains in the JSON snapshot.
                return (
                    "'" + rendered
                    if rendered.lstrip().startswith(("=", "+", "-", "@"))
                    else rendered
                )

            writer.writerow(
                [
                    safe(v)
                    for v in [
                        row["student_id"],
                        row["external_student_id"],
                        row["student_name"],
                        row["status"],
                        row["review_status"],
                        row["suggested_score"],
                        row["confirmed_score"],
                        row["review_revision"],
                        row["answer"]["id"],
                        row["rubric"]["id"],
                        row["evaluation_profile"],
                        row["simulation"],
                    ]
                ]
            )
        return Response(
            stream.getvalue().encode("utf-8-sig"),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="grades-{export_id}.csv"'},
        )

    return app

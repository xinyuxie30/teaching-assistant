from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from teaching_assistant import service as operations
from teaching_assistant.agent import service
from teaching_assistant.agent.contracts import (
    AgentQuery,
    ArtifactConfirmInput,
    CancelInput,
    RunInput,
    WaitInput,
    WakeInput,
)
from teaching_assistant.db import AgentEvent
from teaching_assistant.domain import text_digest
from teaching_assistant.harness import catalog
from teaching_assistant.harness import events as event_stream
from teaching_assistant.harness.grading import GradingRunInput
from teaching_assistant.harness.lesson import LessonRunInput
from teaching_assistant.schemas import json_time


def router(db, identity, settings):
    result = APIRouter()
    TeacherID = Annotated[UUID, Depends(identity)]
    Key = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=120)]

    @result.get("/v1/agent-profiles")
    async def available(
        teacher: TeacherID,
        task_type: Literal[
            "rubric",
            "lesson-plan",
            "lesson-plan-team",
            "lesson-practice",
            "lesson-plan-parallel",
            "lesson-practice-designer",
            "lesson-review",
            "lesson-plan-assisted",
            "lesson-plan-research",
            "lesson-plan-background",
            "class-practice",
            "homework-grading",
        ] = "rubric",
    ):
        return {
            "items": service.profiles(settings, task_type=task_type),
            "default_alias": catalog.ALIASES[task_type][0],
        }

    @result.get("/v1/harness/tasks")
    async def task_catalog(teacher: TeacherID):
        return {"items": catalog.describe_tasks()}

    @result.post("/v1/grading-agent-runs", status_code=202)
    async def create_grading(data: GradingRunInput, teacher: TeacherID, key: Key):
        return await operations.write_operation(
            db,
            teacher,
            "grading-agent",
            key,
            data.model_dump(mode="json", exclude_none=True),
            lambda session: service.create_grading_run(session, teacher, data, settings),
        )

    @result.post("/v1/courses/{course_id}/lesson-agent-runs", status_code=202)
    async def create_lesson(course_id: UUID, data: LessonRunInput, teacher: TeacherID, key: Key):
        return await operations.write_operation(
            db,
            teacher,
            f"lesson-agent:{course_id}",
            key,
            data.model_dump(mode="json", exclude_none=True),
            lambda session: service.create_lesson_run(session, teacher, course_id, data, settings),
        )

    @result.get("/v1/agent-runs/{run_id}/artifact")
    async def artifact(run_id: UUID, teacher: TeacherID):
        async with db.snapshot_sessions.begin() as session:
            _, saved = await service.owned_artifact(session, teacher, run_id)
            return service.artifact_view(saved)

    @result.post("/v1/agent-runs/{run_id}/confirm")
    async def confirm(run_id: UUID, data: ArtifactConfirmInput, teacher: TeacherID, key: Key):
        return await operations.write_operation(
            db,
            teacher,
            f"confirm-agent:{run_id}",
            key,
            data.model_dump(mode="json"),
            lambda session: service.confirm_artifact(session, teacher, run_id, data),
        )

    @result.post("/v1/question-versions/{question_id}/rubric-agent-runs", status_code=202)
    async def create(question_id: UUID, data: RunInput, teacher: TeacherID, key: Key):
        return await operations.write_operation(
            db,
            teacher,
            f"rubric-agent:{question_id}",
            key,
            # Preserve pre-0006 idempotency hashes for ordinary draft requests.
            data.model_dump(mode="json", exclude_none=True),
            lambda session: service.create_run(session, teacher, question_id, data, settings),
        )

    @result.get("/v1/agent-runs")
    async def listing(teacher: TeacherID, params: Annotated[AgentQuery, Query()]):
        async with db.snapshot_sessions.begin() as session:
            return await service.list_runs(session, teacher, params)

    @result.get("/v1/agent-runs/{run_id}")
    async def detail(run_id: UUID, teacher: TeacherID):
        async with db.snapshot_sessions.begin() as session:
            return service.view(await service.owned_run(session, teacher, run_id), detail=True)

    @result.get("/v1/agent-runs/{run_id}/events")
    async def events(
        run_id: UUID,
        teacher: TeacherID,
        after: Annotated[int, Query(ge=0, le=2147483647)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
    ):
        async with db.snapshot_sessions.begin() as session:
            run = await service.owned_run(session, teacher, run_id)
            await catalog.resolve(run.task_type).authorize(session, run)
            rows = list(
                await session.scalars(
                    select(AgentEvent)
                    .where(
                        AgentEvent.run_id == run.id,
                        AgentEvent.sequence > after,
                    )
                    .order_by(AgentEvent.sequence)
                    .limit(limit)
                )
            )
            return {
                "items": [
                    {
                        "sequence": row.sequence,
                        "epoch": row.epoch,
                        "kind": row.kind,
                        "body": row.body,
                        "created_at": json_time(row.created_at),
                    }
                    for row in rows
                ],
                "next_after": rows[-1].sequence if rows else after,
                "has_more": bool(rows and rows[-1].sequence < run.event_seq),
            }

    @result.get("/v1/agent-runs/{run_id}/children")
    async def children(run_id: UUID, teacher: TeacherID):
        async with db.snapshot_sessions.begin() as session:
            return await service.list_children(session, teacher, run_id)

    @result.get("/v1/agent-runs/{run_id}/events/stream")
    async def stream(
        run_id: UUID,
        teacher: TeacherID,
        request: Request,
        after: Annotated[int, Query(ge=0, le=2147483647)] = 0,
        follow_seconds: Annotated[int, Query(ge=0, le=60)] = 25,
        last_event_id: Annotated[str | None, Header(max_length=100)] = None,
    ):
        cursor = event_stream.resume_cursor(run_id, after, last_event_id)
        # Authenticate before opening headers, then recheck identity and course
        # permission in every bounded read, including after a token rotation.
        token = request.headers.get("authorization", "").partition(" ")[2]
        token_hash = text_digest(token)
        await event_stream.read_page(db, teacher, run_id, cursor, token_digest=token_hash)
        return StreamingResponse(
            event_stream.stream_events(
                db, teacher, run_id, cursor, token_digest=token_hash, duration=follow_seconds
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @result.get("/v1/agent-runs/{run_id}/budget")
    async def budget(run_id: UUID, teacher: TeacherID):
        from teaching_assistant.harness import budgets

        async with db.snapshot_sessions.begin() as session:
            run = await service.owned_run(session, teacher, run_id)
            await catalog.resolve(run.task_type).authorize(session, run)
            root = await service.owned_run(session, teacher, run.root_run_id or run.id)
            return await budgets.report(session, root)

    @result.get("/v1/agent-runs/{run_id}/context-checkpoints")
    async def checkpoints(run_id: UUID, teacher: TeacherID):
        async with db.snapshot_sessions.begin() as session:
            return await service.context_checkpoints(session, teacher, run_id)

    @result.post("/v1/agent-runs/{run_id}/cancel")
    async def cancel(run_id: UUID, data: CancelInput, teacher: TeacherID, key: Key):
        return await operations.write_operation(
            db,
            teacher,
            f"cancel-agent:{run_id}",
            key,
            data.model_dump(mode="json"),
            lambda session: service.cancel_run(session, teacher, run_id, data),
        )

    @result.post("/v1/agent-runs/{run_id}/wait")
    async def pause(run_id: UUID, data: WaitInput, teacher: TeacherID, key: Key):
        return await operations.write_operation(
            db,
            teacher,
            f"wait-agent:{run_id}",
            key,
            data.model_dump(mode="json"),
            lambda session: service.wait_run(session, teacher, run_id, data),
        )

    @result.post("/v1/agent-runs/{run_id}/wake")
    async def wake(run_id: UUID, data: WakeInput, teacher: TeacherID, key: Key):
        return await operations.write_operation(
            db,
            teacher,
            f"wake-agent:{run_id}",
            key,
            data.model_dump(mode="json"),
            lambda session: service.wake_run(session, teacher, run_id, data),
        )

    @result.get("/v1/agent-runs/{run_id}/waits")
    async def waits(run_id: UUID, teacher: TeacherID):
        async with db.snapshot_sessions.begin() as session:
            return await service.list_waits(session, teacher, run_id)

    return result

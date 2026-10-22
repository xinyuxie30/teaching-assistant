from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy import select

from teaching_assistant import service as operations
from teaching_assistant.agent import service
from teaching_assistant.agent.contracts import AgentQuery, CancelInput, RunInput
from teaching_assistant.db import AgentEvent
from teaching_assistant.schemas import json_time


def router(db, identity, settings):
    result = APIRouter()
    TeacherID = Annotated[UUID, Depends(identity)]
    Key = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=120)]

    @result.get("/v1/agent-profiles")
    async def available(teacher: TeacherID):
        return {"items": service.profiles(settings), "default_alias": "fake-rubric-agent-v1"}

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
        after: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
    ):
        async with db.snapshot_sessions.begin() as session:
            run = await service.owned_run(session, teacher, run_id)
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

    return result

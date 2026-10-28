"""Teacher-facing bank, retrieval preview and durable practice-run entry points."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query

from teaching_assistant import class_practice as practice
from teaching_assistant import service
from teaching_assistant.schemas import PageQuery


def router(db, identity, settings):
    result = APIRouter()
    TeacherID = Annotated[UUID, Depends(identity)]
    Key = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=120)]
    Params = Annotated[PageQuery, Query()]
    bank = "/v1/courses/{course_id}/practice-questions"
    runs = "/v1/classrooms/{classroom_id}/practice-runs"

    async def read(action, *args):
        async with db.snapshot_sessions.begin() as session:
            return await action(session, *args)

    async def write(teacher, scope, key, data, action):
        return await service.write_operation(
            db, teacher, scope, key, data.model_dump(mode="json"), action, snapshot=True
        )

    @result.get(bank)
    async def list_bank(course_id: UUID, teacher: TeacherID, params: Params):
        return await read(practice.list_bank, teacher, course_id, params)

    @result.post(bank, status_code=201)
    async def import_bank(course_id: UUID, data: practice.BankImport, teacher: TeacherID, key: Key):
        return await write(
            teacher,
            f"practice-bank:{course_id}",
            key,
            data,
            lambda s: practice.import_bank(s, teacher, course_id, data),
        )

    @result.post(bank + "/{question_id}/confirm")
    async def confirm(
        course_id: UUID, question_id: UUID, data: practice.HashInput, teacher: TeacherID, key: Key
    ):
        return await write(
            teacher,
            f"practice-confirm:{question_id}",
            key,
            data,
            lambda s: practice.bank_state(s, teacher, course_id, question_id, data),
        )

    @result.post(bank + "/{question_id}/retire")
    async def retire(
        course_id: UUID, question_id: UUID, data: practice.HashInput, teacher: TeacherID, key: Key
    ):
        return await write(
            teacher,
            f"practice-retire:{question_id}",
            key,
            data,
            lambda s: practice.bank_state(s, teacher, course_id, question_id, data, retire=True),
        )

    @result.get("/v1/classrooms/{classroom_id}/practice-batches")
    async def batches(classroom_id: UUID, teacher: TeacherID, params: Params):
        return await read(practice.list_batches, teacher, classroom_id, params)

    @result.post(runs + "/preview")
    async def preview(classroom_id: UUID, data: practice.PracticeRequest, teacher: TeacherID):
        return await read(practice.preview, teacher, classroom_id, data)

    @result.post(runs, status_code=202)
    async def create_run(
        classroom_id: UUID, data: practice.PracticeRunInput, teacher: TeacherID, key: Key
    ):
        return await write(
            teacher,
            f"class-practice:{classroom_id}",
            key,
            data,
            lambda s: practice.create_run(s, teacher, classroom_id, data, settings),
        )

    @result.get(runs)
    async def runs_list(classroom_id: UUID, teacher: TeacherID, params: Params):
        return await read(practice.list_runs, teacher, classroom_id, params)

    return result

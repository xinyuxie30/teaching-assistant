"""Authenticated course-knowledge commands, separate from model tools."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query

from teaching_assistant import service
from teaching_assistant import teaching_memory as memory


def router(db, identity):
    result = APIRouter()
    TeacherID = Annotated[UUID, Depends(identity)]
    Key = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=120)]
    base = "/v1/courses/{course_id}/teaching-memories"

    async def write(teacher, course_id, key, operation, data, action):
        return await service.write_operation(
            db,
            teacher,
            f"teaching-memory:{course_id}:{operation}",
            key,
            data.model_dump(mode="json"),
            action,
        )

    @result.post(base, status_code=201)
    async def create(course_id: UUID, data: memory.MemoryCreateInput, teacher: TeacherID, key: Key):
        return await write(
            teacher,
            course_id,
            key,
            "create",
            data,
            lambda session: memory.create_memory(session, teacher, course_id, data),
        )

    @result.get(base)
    async def search(
        course_id: UUID,
        teacher: TeacherID,
        query: Annotated[str, Query(max_length=200)] = "",
        limit: Annotated[int, Query(ge=1, le=5)] = 5,
    ):
        async with db.snapshot_sessions.begin() as session:
            return await memory.search_memories(session, teacher, course_id, query, limit)

    @result.get(base + "/{memory_id}")
    async def detail(course_id: UUID, memory_id: UUID, teacher: TeacherID):
        async with db.snapshot_sessions.begin() as session:
            return memory.view(await memory.owned_memory(session, teacher, course_id, memory_id))

    @result.post(base + "/{memory_id}/confirm")
    async def confirm(
        course_id: UUID,
        memory_id: UUID,
        data: memory.MemoryConfirmInput,
        teacher: TeacherID,
        key: Key,
    ):
        return await write(
            teacher,
            course_id,
            key,
            f"confirm:{memory_id}",
            data,
            lambda session: memory.confirm_memory(session, teacher, course_id, memory_id, data),
        )

    @result.post(base + "/{memory_id}/revoke")
    async def revoke(
        course_id: UUID,
        memory_id: UUID,
        data: memory.MemoryRevokeInput,
        teacher: TeacherID,
        key: Key,
    ):
        return await write(
            teacher,
            course_id,
            key,
            f"revoke:{memory_id}",
            data,
            lambda session: memory.revoke_memory(session, teacher, course_id, memory_id, data),
        )

    return result

"""Bounded SSE replay from the durable event log; no model calls or long transactions."""

import asyncio
import json
from time import monotonic
from uuid import UUID

from sqlalchemy import select

from teaching_assistant.agent import service
from teaching_assistant.db import AgentEvent, Teacher
from teaching_assistant.domain import DomainError
from teaching_assistant.harness import catalog
from teaching_assistant.schemas import json_time

TERMINAL = {"waiting_teacher", "completed", "failed", "cancelled"}


def resume_cursor(run_id, after, last_event_id):
    if type(after) is not int or not 0 <= after <= 2147483647:
        raise DomainError(
            "invalid_event_cursor", "Event sequence is outside the supported range", 422
        )
    if not last_event_id:
        return after
    try:
        scope, sequence = last_event_id.split(":")
        if (
            UUID(scope) != run_id
            or not sequence.isascii()
            or not sequence.isdecimal()
            or int(sequence) > 2147483647
        ):
            raise ValueError
        return max(after, int(sequence))
    except (ValueError, TypeError):
        raise DomainError(
            "invalid_event_cursor", "Event cursor belongs to another run or is invalid", 422
        ) from None


def frame(kind, data, event_id=None):
    prefix = f"id: {event_id}\n" if event_id is not None else ""
    return (
        prefix
        + f"event: {kind}\ndata: "
        + json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        + "\n\n"
    )


async def read_page(db, teacher, run_id, after, *, token_digest=None):
    async with db.snapshot_sessions.begin() as session:
        if (
            token_digest is not None
            and await session.scalar(
                select(Teacher.id).where(
                    Teacher.id == teacher, Teacher.token_digest == token_digest
                )
            )
            is None
        ):
            raise DomainError("unauthorized", "Identity has been revoked", 401)
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
                .limit(100)
            )
        )
        return [
            dict(
                sequence=row.sequence,
                epoch=row.epoch,
                kind=row.kind,
                body=row.body,
                created_at=json_time(row.created_at),
            )
            for row in rows
        ], run.status


async def stream_events(db, teacher, run_id, after, *, token_digest, duration=25, poll=0.25):
    deadline, cursor = monotonic() + duration, after
    while True:
        try:
            rows, status = await read_page(db, teacher, run_id, cursor, token_digest=token_digest)
        except DomainError as error:
            yield frame("error", {"code": error.code})
            return
        for row in rows:
            cursor = row["sequence"]
            yield frame("agent_event", row, f"{run_id}:{cursor}")
        if len(rows) < 100 and status in TERMINAL:
            yield frame("end", {"status": status, "after": cursor})
            return
        if monotonic() >= deadline:
            yield frame("reconnect", {"after": cursor})
            return
        if len(rows) == 100:
            continue
        yield ": heartbeat\n\n"
        await asyncio.sleep(min(poll, max(0, deadline - monotonic())))

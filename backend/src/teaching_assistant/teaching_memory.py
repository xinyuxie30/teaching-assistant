"""Teacher-confirmed course knowledge; never a source for independent grading.

Only authenticated application commands can mutate records. Harness tools may call
search_memories with their server-bound identity and course. Retrieved text is data,
not an authority to change task rules or permissions.
"""

from datetime import UTC
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, StringConstraints, ValidationError, model_validator
from sqlalchemy import func, or_, select

from teaching_assistant import service
from teaching_assistant.db import AgentArtifact, AgentRun, TeachingMemory
from teaching_assistant.domain import Contract, DomainError, digest

Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Body = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
Note = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
Hash = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
Kind = Literal["preference", "experience", "material_reference"]
SourceKind = Literal["teacher_note", "confirmed_agent_artifact"]


class MemorySourceInput(Contract):
    kind: SourceKind = "teacher_note"
    note: Note
    artifact_id: UUID | None = None

    @model_validator(mode="after")
    def consistent_source(self):
        if (self.kind == "confirmed_agent_artifact") != (self.artifact_id is not None):
            raise ValueError(
                "Artifact sources require an artifact ID; teacher notes cannot use one"
            )
        return self


class MemorySource(MemorySourceInput):
    content_hash: Hash


class MemoryCreateInput(Contract):
    kind: Kind
    title: Title
    body: Body
    source: MemorySourceInput
    valid_from: AwareDatetime | None = None
    valid_until: AwareDatetime | None = None
    supersedes_id: UUID | None = None

    @model_validator(mode="after")
    def valid_dates(self):
        if self.valid_from and self.valid_until and self.valid_until <= self.valid_from:
            raise ValueError("valid_until must be later than valid_from")
        return self


class MemoryConfirmInput(Contract):
    content_hash: Hash


class MemoryRevokeInput(MemoryConfirmInput):
    reason: Note


class MemorySearchInput(Contract):
    query: str = Field(default="", max_length=200)
    limit: int = Field(default=5, ge=1, le=5, strict=True)


class MemoryView(Contract):
    id: UUID
    course_id: UUID
    kind: Kind
    title: Title
    body: Body
    content_hash: Hash
    source: MemorySource
    valid_from: AwareDatetime
    valid_until: AwareDatetime | None
    confirmed_at: AwareDatetime | None
    revoked_at: AwareDatetime | None
    supersedes_id: UUID | None
    created_at: AwareDatetime


class MemorySearchResult(Contract):
    course_id: UUID
    query: str = Field(max_length=200)
    retrieved_at: AwareDatetime
    items: list[MemoryView] = Field(max_length=5)


def _fixed_body(row):
    return {
        "teacher_id": str(row.teacher_id),
        "course_id": str(row.course_id),
        "kind": row.kind,
        "title": row.title,
        "body": row.body,
        "source": row.source,
        "valid_from": row.valid_from.astimezone(UTC).isoformat(),
        "valid_until": row.valid_until.astimezone(UTC).isoformat() if row.valid_until else None,
        "supersedes_id": str(row.supersedes_id) if row.supersedes_id else None,
    }


def view(row):
    if digest(_fixed_body(row)) != row.content_hash:
        raise DomainError(
            "memory_content_mismatch", "Stored teaching knowledge failed validation", 409
        )
    return MemoryView.model_validate(
        {name: getattr(row, name) for name in MemoryView.model_fields}
    ).model_dump(mode="json")


async def owned_memory(session, teacher, course_id, memory_id, *, lock=False):
    await service.owned_course(session, teacher, course_id)
    if lock:
        # One order for all course-memory writes, including two-record replacement.
        await service.transaction_lock(session, f"teaching-memory:{course_id}")
    query = select(TeachingMemory).where(
        TeachingMemory.id == memory_id,
        TeachingMemory.teacher_id == teacher,
        TeachingMemory.course_id == course_id,
    )
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    row = await session.scalar(query)
    if row is None:
        raise DomainError("not_found", "Teaching knowledge not found", 404)
    view(row)
    return row


async def _source(session, teacher, course_id, source):
    data = source.model_dump(mode="json", exclude={"content_hash"})
    if source.kind == "teacher_note":
        return {**data, "content_hash": digest({"note": source.note})}
    result = (
        await session.execute(
            select(AgentArtifact, AgentRun)
            .join(AgentRun, AgentRun.id == AgentArtifact.run_id)
            .where(AgentArtifact.id == source.artifact_id, AgentRun.teacher_id == teacher)
        )
    ).one_or_none()
    if result is None or result[1].task_input.get("course_id") != str(course_id):
        raise DomainError("not_found", "Confirmed course artifact not found", 404)
    artifact, run = result
    if (
        artifact.confirmed_at is None
        or run.question_version_id is not None
        or artifact.kind != run.task_type
        or not run.task_type.startswith("lesson-plan")
        or artifact.content_hash != digest(artifact.body)
    ):
        raise DomainError(
            "memory_source_unconfirmed", "Source must be a confirmed course plan", 409
        )
    return {**data, "content_hash": artifact.content_hash}


async def validate_source(session, row):
    try:
        source = MemorySource.model_validate(row.source)
    except ValidationError:
        raise DomainError("memory_source_invalid", "Stored source failed validation", 409) from None
    if (
        source.kind != row.source_kind
        or source.artifact_id != row.source_artifact_id
        or await _source(session, row.teacher_id, row.course_id, source) != row.source
    ):
        raise DomainError("memory_source_invalid", "Stored source failed validation", 409)


async def create_memory(session, teacher, course_id, data: MemoryCreateInput):
    await service.owned_course(session, teacher, course_id)
    await service.transaction_lock(session, f"teaching-memory:{course_id}")
    if data.supersedes_id:
        await owned_memory(session, teacher, course_id, data.supersedes_id, lock=True)
    now = await session.scalar(select(func.clock_timestamp()))
    start = (data.valid_from or now).astimezone(UTC)
    end = data.valid_until.astimezone(UTC) if data.valid_until else None
    if end is not None and (end <= start or end <= now):
        raise DomainError("memory_invalid_dates", "New knowledge must have a future expiry")
    source = await _source(session, teacher, course_id, data.source)
    row = TeachingMemory(
        teacher_id=teacher,
        course_id=course_id,
        kind=data.kind,
        title=data.title,
        body=data.body,
        source_kind=data.source.kind,
        source_artifact_id=data.source.artifact_id,
        source=source,
        valid_from=start,
        valid_until=end,
        supersedes_id=data.supersedes_id,
    )
    row.content_hash = digest(_fixed_body(row))
    session.add(row)
    await session.flush()
    service.audit(
        session, teacher, "teaching_memory_proposed", row.id, content_hash=row.content_hash
    )
    return view(row)


async def confirm_memory(session, teacher, course_id, memory_id, data: MemoryConfirmInput):
    row = await owned_memory(session, teacher, course_id, memory_id, lock=True)
    if data.content_hash != row.content_hash:
        raise DomainError("memory_hash_conflict", "Confirm the exact knowledge version viewed", 409)
    if row.revoked_at is not None:
        raise DomainError("memory_revoked", "Revoked knowledge cannot be confirmed", 409)
    if row.confirmed_at is not None:
        return view(row)
    now = await session.scalar(select(func.clock_timestamp()))
    if row.valid_until is not None and row.valid_until <= now:
        raise DomainError("memory_expired", "Expired knowledge cannot be confirmed", 409)
    await validate_source(session, row)
    if row.supersedes_id:
        previous = await owned_memory(session, teacher, course_id, row.supersedes_id, lock=True)
        if previous.revoked_at is not None:
            raise DomainError(
                "memory_superseded_conflict", "The source knowledge was already revoked", 409
            )
        previous.revoked_at = now
        previous.revocation_reason = f"Superseded by {row.id}"
        service.audit(
            session, teacher, "teaching_memory_revoked", previous.id, superseded_by=str(row.id)
        )
    row.confirmed_at = now
    service.audit(
        session, teacher, "teaching_memory_confirmed", row.id, content_hash=row.content_hash
    )
    await session.flush()
    return view(row)


async def revoke_memory(session, teacher, course_id, memory_id, data: MemoryRevokeInput):
    row = await owned_memory(session, teacher, course_id, memory_id, lock=True)
    if data.content_hash != row.content_hash:
        raise DomainError("memory_hash_conflict", "Revoke the exact knowledge version viewed", 409)
    if row.revoked_at is not None:
        if row.revocation_reason != data.reason:
            raise DomainError("memory_revocation_conflict", "Revocation was already recorded", 409)
        return view(row)
    row.revoked_at = await session.scalar(select(func.clock_timestamp()))
    row.revocation_reason = data.reason
    service.audit(session, teacher, "teaching_memory_revoked", row.id, reason=data.reason)
    await session.flush()
    return view(row)


async def search_memories(session, teacher, course_id, query="", limit=5):
    try:
        request = MemorySearchInput(query=query, limit=limit)
    except ValidationError:
        raise DomainError(
            "invalid_memory_query", "Knowledge query or result limit is invalid"
        ) from None
    await service.owned_course(session, teacher, course_id)
    now = await session.scalar(select(func.clock_timestamp()))
    statement = select(TeachingMemory).where(
        TeachingMemory.teacher_id == teacher,
        TeachingMemory.course_id == course_id,
        TeachingMemory.confirmed_at.is_not(None),
        TeachingMemory.revoked_at.is_(None),
        TeachingMemory.valid_from <= now,
        or_(TeachingMemory.valid_until.is_(None), TeachingMemory.valid_until > now),
    )
    if request.query:
        # SQLAlchemy escapes %, _ and the escape character; queries are literal substrings.
        statement = statement.where(
            or_(
                TeachingMemory.title.icontains(request.query, autoescape=True),
                TeachingMemory.body.icontains(request.query, autoescape=True),
            )
        )
    rows = list(
        await session.scalars(
            statement.order_by(TeachingMemory.created_at.desc(), TeachingMemory.id.desc()).limit(
                request.limit
            )
        )
    )
    items = []
    for row in rows:
        await validate_source(session, row)
        items.append(view(row))
    return MemorySearchResult(
        course_id=course_id, query=request.query, retrieved_at=now, items=items
    ).model_dump(mode="json")

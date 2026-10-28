from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Entity:
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Teacher(Entity, Base):
    __tablename__ = "teachers"
    name: Mapped[str] = mapped_column(String(120))
    token_digest: Mapped[str] = mapped_column(String(64), unique=True)


class Course(Entity, Base):
    __tablename__ = "courses"
    teacher_id: Mapped[UUID] = mapped_column(ForeignKey("teachers.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))


class Classroom(Entity, Base):
    __tablename__ = "classrooms"
    course_id: Mapped[UUID] = mapped_column(ForeignKey("courses.id"))
    name: Mapped[str] = mapped_column(String(200))


class TeachingMemory(Entity, Base):
    __tablename__ = "teaching_memories"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('preference','experience','material_reference')", name="ck_memory_kind"
        ),
        CheckConstraint(
            "source_kind IN ('teacher_note','confirmed_agent_artifact') "
            "AND ((source_kind = 'teacher_note' AND source_artifact_id IS NULL) "
            "OR (source_kind = 'confirmed_agent_artifact' AND source_artifact_id IS NOT NULL))",
            name="ck_memory_source",
        ),
        CheckConstraint("valid_until IS NULL OR valid_until > valid_from", name="ck_memory_dates"),
        CheckConstraint(
            "(revoked_at IS NULL) = (revocation_reason IS NULL)", name="ck_memory_revocation"
        ),
        CheckConstraint("supersedes_id IS NULL OR supersedes_id <> id", name="ck_memory_lineage"),
        Index("ix_teaching_memories_scope", "teacher_id", "course_id", "created_at"),
    )
    teacher_id: Mapped[UUID] = mapped_column(ForeignKey("teachers.id"))
    course_id: Mapped[UUID] = mapped_column(ForeignKey("courses.id"))
    kind: Mapped[str] = mapped_column(String(24))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(String(2000))
    source_kind: Mapped[str] = mapped_column(String(32))
    source_artifact_id: Mapped[UUID | None] = mapped_column(ForeignKey("agent_artifacts.id"))
    source: Mapped[dict[str, Any]] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revocation_reason: Mapped[str | None] = mapped_column(String(500))
    supersedes_id: Mapped[UUID | None] = mapped_column(ForeignKey("teaching_memories.id"))


class PracticeQuestion(Entity, Base):
    """Teacher-owned, immutable bank entry; confirmation and retirement are explicit."""

    __tablename__ = "practice_questions"
    __table_args__ = (
        UniqueConstraint("course_id", "content_hash"),
        Index("ix_practice_questions_scope", "teacher_id", "course_id", "created_at"),
    )
    teacher_id: Mapped[UUID] = mapped_column(ForeignKey("teachers.id"))
    course_id: Mapped[UUID] = mapped_column(ForeignKey("courses.id"))
    body: Mapped[dict[str, Any]] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Student(Entity, Base):
    __tablename__ = "students"
    __table_args__ = (UniqueConstraint("course_id", "external_id"),)
    course_id: Mapped[UUID] = mapped_column(ForeignKey("courses.id"))
    external_id: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(120))


class Membership(Base):
    __tablename__ = "memberships"
    classroom_id: Mapped[UUID] = mapped_column(ForeignKey("classrooms.id"), primary_key=True)
    student_id: Mapped[UUID] = mapped_column(ForeignKey("students.id"), primary_key=True)


class Assignment(Entity, Base):
    __tablename__ = "assignments"
    course_id: Mapped[UUID] = mapped_column(ForeignKey("courses.id"), index=True)
    classroom_id: Mapped[UUID] = mapped_column(ForeignKey("classrooms.id"))
    title: Mapped[str] = mapped_column(String(200))


class AssignmentMember(Base):
    __tablename__ = "assignment_members"
    assignment_id: Mapped[UUID] = mapped_column(ForeignKey("assignments.id"), primary_key=True)
    student_id: Mapped[UUID] = mapped_column(ForeignKey("students.id"), primary_key=True)


class QuestionVersion(Entity, Base):
    __tablename__ = "question_versions"
    __table_args__ = (
        UniqueConstraint("assignment_id", "question_key", "version"),
        CheckConstraint("version > 0 AND maximum_points > 0"),
    )
    assignment_id: Mapped[UUID] = mapped_column(ForeignKey("assignments.id"), index=True)
    question_key: Mapped[str] = mapped_column(String(80))
    version: Mapped[int] = mapped_column(Integer)
    prompt: Mapped[str] = mapped_column(Text)
    reference: Mapped[str] = mapped_column(Text)
    maximum_points: Mapped[Decimal] = mapped_column(Numeric(8, 2))
    content_hash: Mapped[str] = mapped_column(String(64))


class RubricDraft(Entity, Base):
    __tablename__ = "rubric_drafts"
    question_version_id: Mapped[UUID] = mapped_column(ForeignKey("question_versions.id"))
    body: Mapped[dict[str, Any]] = mapped_column(JSONB)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    confirmed_version_id: Mapped[UUID | None] = mapped_column(ForeignKey("rubric_versions.id"))


class RubricVersion(Entity, Base):
    __tablename__ = "rubric_versions"
    __table_args__ = (UniqueConstraint("question_version_id", "version"),)
    question_version_id: Mapped[UUID] = mapped_column(ForeignKey("question_versions.id"))
    version: Mapped[int] = mapped_column(Integer)
    body: Mapped[dict[str, Any]] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))
    confirmed_by: Mapped[UUID] = mapped_column(ForeignKey("teachers.id"))


class ImportRecord(Entity, Base):
    __tablename__ = "import_records"
    assignment_id: Mapped[UUID] = mapped_column(ForeignKey("assignments.id"))
    teacher_id: Mapped[UUID] = mapped_column(ForeignKey("teachers.id"))
    request_hash: Mapped[str] = mapped_column(String(64))
    answer_version_ids: Mapped[list[str]] = mapped_column(JSONB)


class AnswerVersion(Entity, Base):
    __tablename__ = "answer_versions"
    __table_args__ = (
        UniqueConstraint(
            "question_version_id", "student_id", "external_submission_id", "source_revision"
        ),
    )
    question_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("question_versions.id"), index=True
    )
    student_id: Mapped[UUID] = mapped_column(ForeignKey("students.id"))
    external_submission_id: Mapped[str] = mapped_column(String(120))
    source_revision: Mapped[str] = mapped_column(String(80))
    answer_text: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GradingBatch(Entity, Base):
    __tablename__ = "grading_batches"
    rubric_version_id: Mapped[UUID] = mapped_column(ForeignKey("rubric_versions.id"))
    teacher_id: Mapped[UUID] = mapped_column(ForeignKey("teachers.id"), index=True)
    status: Mapped[str] = mapped_column(String(24), default="queued")
    outcome: Mapped[str | None] = mapped_column(String(24))
    counts: Mapped[dict[str, int]] = mapped_column(JSONB, default=dict)


class EvaluationProfile(Base):
    __tablename__ = "evaluation_profiles"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True)


class GradingItem(Entity, Base):
    __tablename__ = "grading_items"
    __table_args__ = (
        UniqueConstraint("answer_version_id", "rubric_version_id", "evaluation_profile"),
        CheckConstraint("status IN ('pending','running','retry_wait','succeeded','failed')"),
        CheckConstraint("calls_used >= 0 AND calls_used <= max_calls AND review_revision >= 0"),
        Index("ix_grading_items_ready", "status", "ready_at", "lease_until"),
    )
    answer_version_id: Mapped[UUID] = mapped_column(ForeignKey("answer_versions.id"))
    rubric_version_id: Mapped[UUID] = mapped_column(ForeignKey("rubric_versions.id"))
    teacher_id: Mapped[UUID] = mapped_column(ForeignKey("teachers.id"), index=True)
    evaluation_profile: Mapped[str] = mapped_column(
        String(80), ForeignKey("evaluation_profiles.id")
    )
    status: Mapped[str] = mapped_column(String(24), default="pending")
    ready_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(120))
    epoch: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    calls_used: Mapped[int] = mapped_column(Integer, default=0)
    max_calls: Mapped[int] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(String(120))
    suggested_score: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    review_revision: Mapped[int] = mapped_column(Integer, default=0)


class AgentRun(Entity, Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','running','waiting_children','waiting_input','waiting_timer','waiting_teacher','completed','failed','cancelled')",
            name="ck_agent_run_status",
        ),
        CheckConstraint("models_used >= 0 AND tools_used >= 0 AND event_seq >= 0"),
        CheckConstraint(
            "(source_draft_id IS NULL) = (revision_context IS NULL)",
            name="ck_agent_revision_source",
        ),
        CheckConstraint(
            "task_type <> 'rubric' OR question_version_id IS NOT NULL",
            name="ck_agent_rubric_question",
        ),
        CheckConstraint(
            "(parent_run_id IS NULL AND root_run_id IS NULL AND parent_tool_call_id IS NULL AND depth = 0) "
            "OR (parent_run_id IS NOT NULL AND root_run_id IS NOT NULL AND parent_tool_call_id IS NOT NULL "
            "AND depth > 0 AND parent_run_id <> id AND root_run_id <> id)",
            name="ck_agent_tree_shape",
        ),
        CheckConstraint(
            "tree_models_used >= 0 AND tree_tools_used >= 0 AND children_spawned >= 0",
            name="ck_agent_tree_budget",
        ),
        UniqueConstraint("parent_run_id", "parent_tool_call_id", name="uq_agent_child_call"),
        Index("ix_agent_runs_ready", "status", "ready_at", "lease_until"),
    )
    teacher_id: Mapped[UUID] = mapped_column(ForeignKey("teachers.id"), index=True)
    question_version_id: Mapped[UUID | None] = mapped_column(ForeignKey("question_versions.id"))
    task_type: Mapped[str] = mapped_column(String(80), default="rubric", server_default="rubric")
    task_input: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    task_state: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    parent_run_id: Mapped[UUID | None] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    root_run_id: Mapped[UUID | None] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    parent_tool_call_id: Mapped[str | None] = mapped_column(String(120))
    depth: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    tree_models_used: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    tree_tools_used: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    children_spawned: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    resume_pending: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    wait_state: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    pending_user_messages: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default="[]"
    )
    draft_id: Mapped[UUID | None] = mapped_column(ForeignKey("rubric_drafts.id"))
    source_draft_id: Mapped[UUID | None] = mapped_column(ForeignKey("rubric_drafts.id"))
    revision_context: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    profile_id: Mapped[str] = mapped_column(String(80))
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB)
    limits: Mapped[dict[str, Any]] = mapped_column(JSONB)
    messages: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    pending_calls: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    question_read: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(24), default="queued")
    ready_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(120))
    epoch: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    models_used: Mapped[int] = mapped_column(Integer, default=0)
    tools_used: Mapped[int] = mapped_column(Integer, default=0)
    stop_repairs: Mapped[int] = mapped_column(Integer, default=0)
    event_seq: Mapped[int] = mapped_column(Integer, default=0)
    inflight_seq: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(String(120))


class AgentWait(Entity, Base):
    """Immutable pause snapshot; its id is the token required for one wake receipt."""

    __tablename__ = "agent_waits"
    run_id: Mapped[UUID] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    kind: Mapped[str] = mapped_column(String(24))
    reason: Mapped[str] = mapped_column(String(1000))
    wake_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))


class AgentWake(Entity, Base):
    """One immutable wake or cancellation receipt per pause, shared by API and scanner."""

    __tablename__ = "agent_wakes"
    wait_id: Mapped[UUID] = mapped_column(ForeignKey("agent_waits.id"), unique=True)
    source: Mapped[str] = mapped_column(String(24))
    response: Mapped[str | None] = mapped_column(Text)
    request_hash: Mapped[str] = mapped_column(String(64))


class AgentContextCheckpoint(Entity, Base):
    __tablename__ = "agent_context_checkpoints"
    __table_args__ = (UniqueConstraint("run_id", "sequence"),)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    source_hash: Mapped[str] = mapped_column(String(64))
    messages: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB)


class AgentArtifact(Entity, Base):
    """One immutable proposed artifact per run; confirmation is an explicit teacher operation."""

    __tablename__ = "agent_artifacts"
    run_id: Mapped[UUID] = mapped_column(ForeignKey("agent_runs.id"), unique=True)
    kind: Mapped[str] = mapped_column(String(80))
    body: Mapped[dict[str, Any]] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentToolResult(Entity, Base):
    """Immutable large-result receipts. Reading is confined to the originating run."""

    __tablename__ = "agent_tool_results"
    __table_args__ = (
        UniqueConstraint("run_id", "tool_call_id"),
        CheckConstraint("character_count > 0", name="ck_tool_result_length"),
    )
    run_id: Mapped[UUID] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    teacher_id: Mapped[UUID] = mapped_column(ForeignKey("teachers.id"))
    tool_call_id: Mapped[str] = mapped_column(String(120))
    tool_name: Mapped[str] = mapped_column(String(120))
    arguments_hash: Mapped[str] = mapped_column(String(64))
    body: Mapped[dict[str, Any]] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))
    character_count: Mapped[int] = mapped_column(Integer)


class AgentEvent(Entity, Base):
    __tablename__ = "agent_events"
    __table_args__ = (UniqueConstraint("run_id", "sequence"),)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    epoch: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(40))
    body: Mapped[dict[str, Any]] = mapped_column(JSONB)


class AgentModelReservation(Entity, Base):
    """Immutable request reservation with one final reported-usage settlement."""

    __tablename__ = "agent_model_reservations"
    __table_args__ = (
        UniqueConstraint("run_id", "dispatch_sequence", name="uq_agent_reservation_dispatch"),
        ForeignKeyConstraint(
            ["run_id", "dispatch_sequence"],
            ["agent_events.run_id", "agent_events.sequence"],
            name="fk_agent_reservation_dispatch",
        ),
        CheckConstraint(
            "reserved_input_tokens >= 0 AND reserved_output_tokens > 0 "
            "AND (reserved_cost_usd IS NULL OR reserved_cost_usd >= 0)",
            name="ck_agent_reservation_bounds",
        ),
        CheckConstraint(
            "(settled_at IS NULL AND actual_input_tokens IS NULL AND actual_output_tokens IS NULL "
            "AND settled_cost_usd IS NULL AND cost_is_exact IS NULL AND usage IS NULL) OR "
            "(settled_at IS NOT NULL AND actual_input_tokens IS NOT NULL AND actual_output_tokens IS NOT NULL "
            "AND actual_input_tokens >= 0 AND actual_output_tokens >= 0 "
            "AND usage IS NOT NULL AND ((settled_cost_usd IS NULL AND cost_is_exact IS NULL) OR "
            "(settled_cost_usd >= 0 AND cost_is_exact IS NOT NULL)))",
            name="ck_agent_reservation_settlement",
        ),
    )
    root_run_id: Mapped[UUID] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    dispatch_sequence: Mapped[int] = mapped_column(Integer)
    request_hash: Mapped[str] = mapped_column(String(64))
    policy_hash: Mapped[str] = mapped_column(String(64))
    policy: Mapped[dict[str, Any]] = mapped_column(JSONB)
    reserved_input_tokens: Mapped[int] = mapped_column(Integer)
    reserved_output_tokens: Mapped[int] = mapped_column(Integer)
    reserved_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(24, 12))
    actual_input_tokens: Mapped[int | None] = mapped_column(Integer)
    actual_output_tokens: Mapped[int | None] = mapped_column(Integer)
    settled_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(24, 12))
    cost_is_exact: Mapped[bool | None] = mapped_column(Boolean)
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BatchItem(Base):
    __tablename__ = "batch_items"
    batch_id: Mapped[UUID] = mapped_column(ForeignKey("grading_batches.id"), primary_key=True)
    item_id: Mapped[UUID] = mapped_column(
        ForeignKey("grading_items.id"), primary_key=True, index=True
    )


class Attempt(Entity, Base):
    __tablename__ = "attempts"
    __table_args__ = (UniqueConstraint("item_id", "epoch"),)
    item_id: Mapped[UUID] = mapped_column(ForeignKey("grading_items.id"), index=True)
    epoch: Mapped[int] = mapped_column(Integer)
    worker_id: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(24))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ModelCall(Entity, Base):
    __tablename__ = "model_calls"
    item_id: Mapped[UUID] = mapped_column(ForeignKey("grading_items.id"), index=True)
    epoch: Mapped[int] = mapped_column(Integer)
    criterion_id: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(24))
    input_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24))
    requested_model: Mapped[str] = mapped_column(String(120))
    returned_model: Mapped[str | None] = mapped_column(String(120))
    adapter_version: Mapped[str | None] = mapped_column(String(120))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(120))
    provider_request_id: Mapped[str | None] = mapped_column(String(255))
    provider_fingerprint: Mapped[str | None] = mapped_column(String(255))
    prompt_hash: Mapped[str | None] = mapped_column(String(64))
    http_status: Mapped[int | None] = mapped_column(Integer)
    error_retryable: Mapped[bool | None] = mapped_column(Boolean)


class Step(Entity, Base):
    __tablename__ = "steps"
    __table_args__ = (UniqueConstraint("item_id", "criterion_id", "kind"),)
    item_id: Mapped[UUID] = mapped_column(ForeignKey("grading_items.id"), index=True)
    criterion_id: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(24))
    call_id: Mapped[UUID] = mapped_column(ForeignKey("model_calls.id"))
    output: Mapped[dict[str, Any]] = mapped_column(JSONB)
    input_hash: Mapped[str] = mapped_column(String(64))


class Assessment(Entity, Base):
    __tablename__ = "assessments"
    __table_args__ = (UniqueConstraint("item_id", "criterion_id"),)
    item_id: Mapped[UUID] = mapped_column(ForeignKey("grading_items.id"), index=True)
    criterion_id: Mapped[str] = mapped_column(String(80))
    level_id: Mapped[str | None] = mapped_column(String(80))
    suggested_points: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    evaluation_step_id: Mapped[UUID] = mapped_column(ForeignKey("steps.id"))
    evidence_step_id: Mapped[UUID] = mapped_column(ForeignKey("steps.id"))
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB)
    issues: Mapped[list[str]] = mapped_column(JSONB)


class Review(Entity, Base):
    __tablename__ = "reviews"
    __table_args__ = (UniqueConstraint("item_id", "revision"),)
    item_id: Mapped[UUID] = mapped_column(ForeignKey("grading_items.id"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    teacher_id: Mapped[UUID] = mapped_column(ForeignKey("teachers.id"))
    decisions: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    confirmed_score: Mapped[Decimal] = mapped_column(Numeric(8, 2))


class ExportSnapshot(Entity, Base):
    __tablename__ = "export_snapshots"
    batch_id: Mapped[UUID] = mapped_column(ForeignKey("grading_batches.id"))
    teacher_id: Mapped[UUID] = mapped_column(ForeignKey("teachers.id"), index=True)
    body: Mapped[dict[str, Any]] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))


class Receipt(Entity, Base):
    __tablename__ = "operation_receipts"
    __table_args__ = (UniqueConstraint("teacher_id", "scope", "key"),)
    teacher_id: Mapped[UUID] = mapped_column(ForeignKey("teachers.id"))
    scope: Mapped[str] = mapped_column(String(200))
    key: Mapped[str] = mapped_column(String(120))
    request_hash: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class Audit(Entity, Base):
    __tablename__ = "audit_events"
    teacher_id: Mapped[UUID] = mapped_column(ForeignKey("teachers.id"))
    action: Mapped[str] = mapped_column(String(80))
    object_id: Mapped[UUID] = mapped_column(index=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class Database:
    def __init__(self, url: str, *, schema: str | None = None):
        connect_args = {"server_settings": {"search_path": schema}} if schema else {}
        self.engine = create_async_engine(url, pool_pre_ping=True, connect_args=connect_args)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.snapshot_sessions = async_sessionmaker(
            self.engine.execution_options(isolation_level="REPEATABLE READ"),
            expire_on_commit=False,
        )

    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessions() as session:
            yield session

    async def close(self) -> None:
        await self.engine.dispose()

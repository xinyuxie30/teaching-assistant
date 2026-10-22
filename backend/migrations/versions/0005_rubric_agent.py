"""Durable rubric drafting agent runs and append-only event receipts.

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("teacher_id", sa.Uuid(), sa.ForeignKey("teachers.id"), nullable=False),
        sa.Column(
            "question_version_id", sa.Uuid(), sa.ForeignKey("question_versions.id"), nullable=False
        ),
        sa.Column("draft_id", sa.Uuid(), sa.ForeignKey("rubric_drafts.id")),
        sa.Column("profile_id", sa.String(80), nullable=False),
        sa.Column("manifest", JSONB(), nullable=False),
        sa.Column("limits", JSONB(), nullable=False),
        sa.Column("messages", JSONB(), nullable=False),
        sa.Column("pending_calls", JSONB(), nullable=False),
        sa.Column("question_read", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column(
            "ready_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("lease_owner", sa.String(120)),
        *[
            sa.Column(name, sa.Integer(), nullable=False)
            for name in (
                "epoch",
                "attempts",
                "models_used",
                "tools_used",
                "stop_repairs",
                "event_seq",
            )
        ],
        sa.Column("inflight_seq", sa.Integer()),
        sa.Column("last_error", sa.String(120)),
        sa.CheckConstraint(
            "status IN ('queued','running','waiting_teacher','completed','failed','cancelled')"
        ),
        sa.CheckConstraint("models_used >= 0 AND tools_used >= 0 AND event_seq >= 0"),
    )
    op.create_index("ix_agent_runs_teacher_id", "agent_runs", ["teacher_id"])
    op.create_index("ix_agent_runs_ready", "agent_runs", ["status", "ready_at", "lease_until"])
    op.create_table(
        "agent_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("agent_runs.id"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("body", JSONB(), nullable=False),
        sa.UniqueConstraint("run_id", "sequence"),
    )
    op.create_index("ix_agent_events_run_id", "agent_events", ["run_id"])
    op.execute(
        "CREATE TRIGGER protect_frozen_record BEFORE UPDATE OR DELETE ON agent_events "
        "FOR EACH ROW EXECUTE FUNCTION reject_frozen_mutation()"
    )


def downgrade():
    op.drop_table("agent_events")
    op.drop_table("agent_runs")

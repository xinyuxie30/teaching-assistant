"""Durable external waits, frozen time budgets, and immutable wake receipts."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def replace_status(include_waits):
    op.drop_constraint("ck_agent_run_status", "agent_runs", type_="check")
    values = (
        "'queued','running','waiting_children','waiting_teacher','completed','failed','cancelled'"
    )
    if include_waits:
        values += ",'waiting_input','waiting_timer'"
    op.create_check_constraint("ck_agent_run_status", "agent_runs", f"status IN ({values})")


def upgrade():
    replace_status(True)
    op.add_column(
        "agent_runs", sa.Column("wait_state", JSONB(), server_default="{}", nullable=False)
    )
    op.add_column(
        "agent_runs",
        sa.Column("pending_user_messages", JSONB(), server_default="[]", nullable=False),
    )
    op.create_table(
        "agent_waits",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("agent_runs.id"), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("reason", sa.String(1000), nullable=False),
        sa.Column("wake_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("snapshot", JSONB(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
    )
    op.create_index("ix_agent_waits_run_id", "agent_waits", ["run_id"])
    op.create_index("ix_agent_waits_wake_at", "agent_waits", ["wake_at"])
    op.create_table(
        "agent_wakes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("wait_id", sa.Uuid(), sa.ForeignKey("agent_waits.id"), nullable=False),
        sa.Column("source", sa.String(24), nullable=False),
        sa.Column("response", sa.Text(), nullable=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.UniqueConstraint("wait_id"),
    )
    for table in ("agent_waits", "agent_wakes"):
        op.execute(
            f"CREATE TRIGGER protect_frozen_record BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_frozen_mutation()"
        )


def downgrade():
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM agent_waits)
               OR EXISTS (SELECT 1 FROM agent_wakes)
               OR EXISTS (SELECT 1 FROM agent_runs WHERE wait_state <> '{}'::jsonb
                          OR pending_user_messages <> '[]'::jsonb
                          OR status IN ('waiting_input','waiting_timer')) THEN
                RAISE EXCEPTION 'Cannot downgrade while durable wait history exists';
            END IF;
        END $$;
    """)
    op.drop_table("agent_wakes")
    op.drop_table("agent_waits")
    op.drop_column("agent_runs", "pending_user_messages")
    op.drop_column("agent_runs", "wait_state")
    replace_status(False)

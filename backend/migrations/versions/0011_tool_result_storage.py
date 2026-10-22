"""Immutable scoped storage for explicitly externalizable tool results."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "agent_tool_results",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("agent_runs.id"), nullable=False),
        sa.Column("teacher_id", sa.Uuid(), sa.ForeignKey("teachers.id"), nullable=False),
        sa.Column("tool_call_id", sa.String(120), nullable=False),
        sa.Column("tool_name", sa.String(120), nullable=False),
        sa.Column("arguments_hash", sa.String(64), nullable=False),
        sa.Column("body", JSONB(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("character_count", sa.Integer(), nullable=False),
        sa.UniqueConstraint("run_id", "tool_call_id"),
        sa.CheckConstraint("character_count > 0", name="ck_tool_result_length"),
    )
    op.create_index("ix_agent_tool_results_run_id", "agent_tool_results", ["run_id"])
    op.execute("""
        CREATE FUNCTION protect_tool_result_scope() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM agent_runs WHERE id = NEW.run_id AND teacher_id = NEW.teacher_id) THEN
                RAISE EXCEPTION 'Tool result scope differs from its run' USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$;
    """)
    op.execute(
        "CREATE TRIGGER protect_tool_result_scope BEFORE INSERT ON agent_tool_results "
        "FOR EACH ROW EXECUTE FUNCTION protect_tool_result_scope()"
    )
    op.execute(
        "CREATE TRIGGER protect_frozen_record BEFORE UPDATE OR DELETE ON agent_tool_results "
        "FOR EACH ROW EXECUTE FUNCTION reject_frozen_mutation()"
    )


def downgrade():
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM agent_tool_results) THEN
                RAISE EXCEPTION 'Cannot downgrade while tool result history exists';
            END IF;
        END $$;
    """)
    op.drop_table("agent_tool_results")
    op.execute("DROP FUNCTION protect_tool_result_scope()")

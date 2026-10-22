"""Task-independent harness state and immutable proposed artifacts.

Revision ID: 0007
Revises: 0006
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "agent_runs",
        sa.Column("task_type", sa.String(80), server_default="rubric", nullable=False),
    )
    for name in ("task_input", "task_state"):
        op.add_column("agent_runs", sa.Column(name, JSONB(), server_default="{}", nullable=False))
    op.alter_column("agent_runs", "question_version_id", nullable=True)
    op.create_check_constraint(
        "ck_agent_rubric_question",
        "agent_runs",
        "task_type <> 'rubric' OR question_version_id IS NOT NULL",
    )
    op.create_table(
        "agent_artifacts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("agent_runs.id"), nullable=False),
        sa.Column("kind", sa.String(80), nullable=False),
        sa.Column("body", JSONB(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("run_id"),
    )
    op.execute("""
        CREATE FUNCTION protect_harness_input() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.task_type IS DISTINCT FROM OLD.task_type
               OR NEW.task_input IS DISTINCT FROM OLD.task_input THEN
                RAISE EXCEPTION 'Harness task identity and input are immutable'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$;
    """)
    op.execute(
        "CREATE TRIGGER protect_harness_input BEFORE UPDATE ON agent_runs "
        "FOR EACH ROW EXECUTE FUNCTION protect_harness_input()"
    )
    op.execute("""
        CREATE FUNCTION protect_harness_artifact() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'Harness artifacts are immutable' USING ERRCODE = '55000';
            END IF;
            IF (to_jsonb(NEW) - 'confirmed_at') IS DISTINCT FROM
               (to_jsonb(OLD) - 'confirmed_at')
               OR (OLD.confirmed_at IS NOT NULL AND
                   NEW.confirmed_at IS DISTINCT FROM OLD.confirmed_at) THEN
                RAISE EXCEPTION 'Harness artifacts only allow first confirmation'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$;
    """)
    op.execute(
        "CREATE TRIGGER protect_harness_artifact BEFORE UPDATE OR DELETE ON agent_artifacts "
        "FOR EACH ROW EXECUTE FUNCTION protect_harness_artifact()"
    )


def downgrade():
    # A rollback must never silently discard non-rubric tasks or their proposed work.
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM agent_runs WHERE task_type <> 'rubric')
               OR EXISTS (SELECT 1 FROM agent_artifacts) THEN
                RAISE EXCEPTION 'Cannot downgrade while generic harness tasks or artifacts exist';
            END IF;
        END $$;
    """)
    op.drop_table("agent_artifacts")
    op.execute("DROP FUNCTION protect_harness_artifact()")
    op.execute("DROP TRIGGER protect_harness_input ON agent_runs")
    op.execute("DROP FUNCTION protect_harness_input()")
    op.drop_constraint("ck_agent_rubric_question", "agent_runs", type_="check")
    op.alter_column("agent_runs", "question_version_id", nullable=False)
    for name in ("task_state", "task_input", "task_type"):
        op.drop_column("agent_runs", name)

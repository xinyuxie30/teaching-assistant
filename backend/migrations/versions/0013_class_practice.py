"""Course question bank for evidence-grounded class practice."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "practice_questions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("teacher_id", sa.Uuid(), sa.ForeignKey("teachers.id"), nullable=False),
        sa.Column("course_id", sa.Uuid(), sa.ForeignKey("courses.id"), nullable=False),
        sa.Column("body", JSONB(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        sa.Column("retired_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("course_id", "content_hash"),
    )
    op.create_index(
        "ix_practice_questions_scope",
        "practice_questions",
        ["teacher_id", "course_id", "created_at"],
    )
    op.execute("""
        CREATE FUNCTION protect_practice_question() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'Question history cannot be deleted' USING ERRCODE = '55000';
            END IF;
            IF TG_OP = 'INSERT' THEN
                IF NEW.confirmed_at IS NOT NULL OR NEW.retired_at IS NOT NULL OR NOT EXISTS (
                    SELECT 1 FROM courses WHERE id = NEW.course_id AND teacher_id = NEW.teacher_id
                ) THEN
                    RAISE EXCEPTION 'Invalid question scope or initial state' USING ERRCODE = '55000';
                END IF;
                RETURN NEW;
            END IF;
            IF (to_jsonb(NEW) - ARRAY['confirmed_at','retired_at']) IS DISTINCT FROM
               (to_jsonb(OLD) - ARRAY['confirmed_at','retired_at'])
               OR (OLD.confirmed_at IS NOT NULL AND NEW.confirmed_at IS DISTINCT FROM OLD.confirmed_at)
               OR (OLD.retired_at IS NOT NULL AND (
                   NEW.retired_at IS DISTINCT FROM OLD.retired_at OR
                   NEW.confirmed_at IS DISTINCT FROM OLD.confirmed_at)) THEN
                RAISE EXCEPTION 'Only first confirmation and retirement allowed' USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END; $$;
    """)
    op.execute(
        "CREATE TRIGGER protect_practice_question BEFORE INSERT OR UPDATE OR DELETE ON practice_questions FOR EACH ROW EXECUTE FUNCTION protect_practice_question()"
    )


def downgrade():
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM practice_questions) OR EXISTS (
            SELECT 1 FROM agent_runs WHERE task_type = 'class-practice'
        ) THEN RAISE EXCEPTION 'Cannot discard practice history'; END IF;
    END $$;""")
    op.drop_table("practice_questions")
    op.execute("DROP FUNCTION protect_practice_question()")

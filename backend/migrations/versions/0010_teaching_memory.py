"""Scoped, immutable teaching knowledge with explicit confirmation and revocation."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "teaching_memories",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("teacher_id", sa.Uuid(), sa.ForeignKey("teachers.id"), nullable=False),
        sa.Column("course_id", sa.Uuid(), sa.ForeignKey("courses.id"), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("body", sa.String(2000), nullable=False),
        sa.Column("source_kind", sa.String(32), nullable=False),
        sa.Column("source_artifact_id", sa.Uuid(), sa.ForeignKey("agent_artifacts.id")),
        sa.Column("source", JSONB(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True)),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("revocation_reason", sa.String(500)),
        sa.Column("supersedes_id", sa.Uuid(), sa.ForeignKey("teaching_memories.id")),
        sa.CheckConstraint(
            "kind IN ('preference','experience','material_reference')", name="ck_memory_kind"
        ),
        sa.CheckConstraint(
            "source_kind IN ('teacher_note','confirmed_agent_artifact') "
            "AND ((source_kind = 'teacher_note' AND source_artifact_id IS NULL) "
            "OR (source_kind = 'confirmed_agent_artifact' AND source_artifact_id IS NOT NULL))",
            name="ck_memory_source",
        ),
        sa.CheckConstraint(
            "valid_until IS NULL OR valid_until > valid_from", name="ck_memory_dates"
        ),
        sa.CheckConstraint(
            "(revoked_at IS NULL) = (revocation_reason IS NULL)", name="ck_memory_revocation"
        ),
        sa.CheckConstraint(
            "supersedes_id IS NULL OR supersedes_id <> id", name="ck_memory_lineage"
        ),
    )
    op.create_index(
        "ix_teaching_memories_scope", "teaching_memories", ["teacher_id", "course_id", "created_at"]
    )
    op.execute("""
        CREATE FUNCTION protect_teaching_memory() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'Teaching memory history cannot be deleted' USING ERRCODE = '55000';
            END IF;
            IF TG_OP = 'INSERT' THEN
                IF NEW.confirmed_at IS NOT NULL OR NEW.revoked_at IS NOT NULL THEN
                    RAISE EXCEPTION 'Teaching memory must begin unconfirmed' USING ERRCODE = '55000';
                END IF;
                IF NOT EXISTS (SELECT 1 FROM courses WHERE id = NEW.course_id AND teacher_id = NEW.teacher_id)
                   OR (NEW.supersedes_id IS NOT NULL AND NOT EXISTS (
                       SELECT 1 FROM teaching_memories WHERE id = NEW.supersedes_id
                       AND teacher_id = NEW.teacher_id AND course_id = NEW.course_id)) THEN
                    RAISE EXCEPTION 'Teaching memory scope mismatch' USING ERRCODE = '55000';
                END IF;
                RETURN NEW;
            END IF;
            IF (to_jsonb(NEW) - ARRAY['confirmed_at','revoked_at','revocation_reason'])
               IS DISTINCT FROM
               (to_jsonb(OLD) - ARRAY['confirmed_at','revoked_at','revocation_reason'])
               OR (OLD.confirmed_at IS NOT NULL AND NEW.confirmed_at IS DISTINCT FROM OLD.confirmed_at)
               OR (OLD.revoked_at IS NOT NULL AND (
                   NEW.revoked_at IS DISTINCT FROM OLD.revoked_at
                   OR NEW.revocation_reason IS DISTINCT FROM OLD.revocation_reason
                   OR NEW.confirmed_at IS DISTINCT FROM OLD.confirmed_at)) THEN
                RAISE EXCEPTION 'Teaching memory only allows first confirmation and revocation'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$;
    """)
    op.execute(
        "CREATE TRIGGER protect_teaching_memory BEFORE INSERT OR UPDATE OR DELETE ON teaching_memories "
        "FOR EACH ROW EXECUTE FUNCTION protect_teaching_memory()"
    )


def downgrade():
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM teaching_memories) THEN
                RAISE EXCEPTION 'Cannot downgrade while teaching memory history exists';
            END IF;
        END $$;
    """)
    op.drop_table("teaching_memories")
    op.execute("DROP FUNCTION protect_teaching_memory()")

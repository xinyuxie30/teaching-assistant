"""Reject mutation of fixed inputs, accepted observations and review history.

Revision ID: 0002
Revises: 0001
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

TABLES = (
    "question_versions",
    "rubric_versions",
    "answer_versions",
    "steps",
    "assessments",
    "reviews",
    "export_snapshots",
    "import_records",
    "audit_events",
    "assignment_members",
)


def upgrade():
    op.execute("""
        CREATE FUNCTION reject_frozen_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '% rows are immutable; create a new version', TG_TABLE_NAME
                USING ERRCODE = '55000';
        END;
        $$;
    """)
    for table in TABLES:
        op.execute(
            f"CREATE TRIGGER protect_frozen_record BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_frozen_mutation()"
        )


def downgrade():
    for table in TABLES:
        op.execute(f"DROP TRIGGER protect_frozen_record ON {table}")
    op.execute("DROP FUNCTION reject_frozen_mutation()")

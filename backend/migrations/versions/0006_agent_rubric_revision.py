"""Snapshot the source draft and teacher feedback for a new agent run.

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("agent_runs", sa.Column("source_draft_id", sa.Uuid(), nullable=True))
    op.add_column("agent_runs", sa.Column("revision_context", JSONB(), nullable=True))
    op.create_foreign_key(
        "fk_agent_runs_source_draft", "agent_runs", "rubric_drafts", ["source_draft_id"], ["id"]
    )
    op.create_check_constraint(
        "ck_agent_revision_source",
        "agent_runs",
        "(source_draft_id IS NULL) = (revision_context IS NULL)",
    )


def downgrade():
    op.drop_constraint("ck_agent_revision_source", "agent_runs", type_="check")
    op.drop_constraint("fk_agent_runs_source_draft", "agent_runs", type_="foreignkey")
    op.drop_column("agent_runs", "revision_context")
    op.drop_column("agent_runs", "source_draft_id")

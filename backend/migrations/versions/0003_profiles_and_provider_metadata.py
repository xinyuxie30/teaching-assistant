"""Freeze evaluation profiles and retain provider call metadata.

Revision ID: 0003
Revises: 0002
"""

import hashlib
import json

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

# Keep this migration self-contained; later application prompt changes cannot rewrite history.
FAKE_MANIFEST = {
    "schema": "evaluation-profile-v1",
    "provider": "fake",
    "model": "fake-v1",
    "adapter_version": "fake-v1",
    "prompt_version": "fake-v1",
    "evidence_strategy": "fake-prefix-v1",
    "simulation": True,
}


def upgrade():
    profiles = op.create_table(
        "evaluation_profiles",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("manifest", postgresql.JSONB(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.UniqueConstraint("fingerprint"),
    )
    canonical = json.dumps(FAKE_MANIFEST, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    op.bulk_insert(
        profiles,
        [
            {
                "id": "fake-v1",
                "manifest": FAKE_MANIFEST,
                "fingerprint": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            }
        ],
    )
    op.create_foreign_key(
        "fk_grading_items_evaluation_profile",
        "grading_items",
        "evaluation_profiles",
        ["evaluation_profile"],
        ["id"],
    )
    op.execute(
        "CREATE TRIGGER protect_frozen_record BEFORE UPDATE OR DELETE ON evaluation_profiles "
        "FOR EACH ROW EXECUTE FUNCTION reject_frozen_mutation()"
    )
    op.add_column("model_calls", sa.Column("provider_request_id", sa.String(255)))
    op.add_column("model_calls", sa.Column("prompt_hash", sa.String(64)))
    op.add_column("model_calls", sa.Column("http_status", sa.Integer()))
    op.add_column("model_calls", sa.Column("error_retryable", sa.Boolean()))


def downgrade():
    op.drop_column("model_calls", "error_retryable")
    op.drop_column("model_calls", "http_status")
    op.drop_column("model_calls", "prompt_hash")
    op.drop_column("model_calls", "provider_request_id")
    op.drop_constraint("fk_grading_items_evaluation_profile", "grading_items", type_="foreignkey")
    op.drop_table("evaluation_profiles")

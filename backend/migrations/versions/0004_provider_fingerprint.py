"""Retain provider fingerprints on successful and failed call receipts.

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("model_calls", sa.Column("provider_fingerprint", sa.String(255)))
    # Only copy observations already saved on the matching accepted checkpoint.
    # No fabricated fingerprints for legacy failures or unknown calls; no frozen-row edits.
    op.execute("""
        UPDATE model_calls AS receipt
        SET provider_fingerprint = left(
            CASE step.kind
                WHEN 'evaluate' THEN step.output ->> 'provider_fingerprint'
                WHEN 'extract_evidence' THEN
                    step.output #>> '{model_metadata,provider_fingerprint}'
            END, 255
        )
        FROM steps AS step
        WHERE step.call_id = receipt.id
          AND step.item_id = receipt.item_id
          AND step.criterion_id = receipt.criterion_id
          AND step.kind = receipt.kind
          AND step.input_hash = receipt.input_hash
          AND receipt.status = 'succeeded'
          AND receipt.provider_fingerprint IS NULL
          AND jsonb_typeof(
              CASE step.kind
                  WHEN 'evaluate' THEN step.output -> 'provider_fingerprint'
                  WHEN 'extract_evidence' THEN
                      step.output #> '{model_metadata,provider_fingerprint}'
              END
          ) = 'string'
    """)


def downgrade():
    op.drop_column("model_calls", "provider_fingerprint")

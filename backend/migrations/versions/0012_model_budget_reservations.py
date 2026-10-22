"""Durable root-scoped model reservations and immutable reported-usage settlements."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "agent_model_reservations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("root_run_id", sa.Uuid(), sa.ForeignKey("agent_runs.id"), nullable=False),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("agent_runs.id"), nullable=False),
        sa.Column("dispatch_sequence", sa.Integer(), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("policy_hash", sa.String(64), nullable=False),
        sa.Column("policy", JSONB(), nullable=False),
        sa.Column("reserved_input_tokens", sa.Integer(), nullable=False),
        sa.Column("reserved_output_tokens", sa.Integer(), nullable=False),
        sa.Column("reserved_cost_usd", sa.Numeric(24, 12)),
        sa.Column("actual_input_tokens", sa.Integer()),
        sa.Column("actual_output_tokens", sa.Integer()),
        sa.Column("settled_cost_usd", sa.Numeric(24, 12)),
        sa.Column("cost_is_exact", sa.Boolean()),
        sa.Column("usage", JSONB(none_as_null=True)),
        sa.Column("settled_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("run_id", "dispatch_sequence", name="uq_agent_reservation_dispatch"),
        sa.ForeignKeyConstraint(
            ["run_id", "dispatch_sequence"],
            ["agent_events.run_id", "agent_events.sequence"],
            name="fk_agent_reservation_dispatch",
        ),
        sa.CheckConstraint(
            "reserved_input_tokens >= 0 AND reserved_output_tokens > 0 "
            "AND (reserved_cost_usd IS NULL OR reserved_cost_usd >= 0)",
            name="ck_agent_reservation_bounds",
        ),
        sa.CheckConstraint(
            "(settled_at IS NULL AND actual_input_tokens IS NULL AND actual_output_tokens IS NULL "
            "AND settled_cost_usd IS NULL AND cost_is_exact IS NULL AND usage IS NULL) OR "
            "(settled_at IS NOT NULL AND actual_input_tokens IS NOT NULL AND actual_output_tokens IS NOT NULL "
            "AND actual_input_tokens >= 0 AND actual_output_tokens >= 0 "
            "AND usage IS NOT NULL AND ((settled_cost_usd IS NULL AND cost_is_exact IS NULL) OR "
            "(settled_cost_usd >= 0 AND cost_is_exact IS NOT NULL)))",
            name="ck_agent_reservation_settlement",
        ),
    )
    op.create_index(
        "ix_agent_model_reservations_root_run_id", "agent_model_reservations", ["root_run_id"]
    )
    op.create_index("ix_agent_model_reservations_run_id", "agent_model_reservations", ["run_id"])
    op.execute("""
        CREATE FUNCTION protect_agent_model_reservation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'Model reservation history cannot be deleted' USING ERRCODE = '55000';
            END IF;
            IF TG_OP = 'INSERT' THEN
                IF NEW.settled_at IS NOT NULL THEN
                    RAISE EXCEPTION 'Model usage must follow its reservation' USING ERRCODE = '55000';
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM agent_runs child JOIN agent_runs root ON root.id = NEW.root_run_id
                    WHERE child.id = NEW.run_id AND COALESCE(child.root_run_id, child.id) = root.id
                    AND root.parent_run_id IS NULL AND root.root_run_id IS NULL
                    AND child.teacher_id = root.teacher_id
                ) OR NOT EXISTS (
                    SELECT 1 FROM agent_events WHERE run_id = NEW.run_id
                    AND sequence = NEW.dispatch_sequence AND kind = 'model_dispatched'
                ) THEN
                    RAISE EXCEPTION 'Model reservation scope or dispatch mismatch' USING ERRCODE = '55000';
                END IF;
                RETURN NEW;
            END IF;
            IF OLD.settled_at IS NOT NULL
               OR (to_jsonb(NEW) - ARRAY['settled_at','actual_input_tokens','actual_output_tokens','settled_cost_usd','cost_is_exact','usage'])
                  IS DISTINCT FROM
                  (to_jsonb(OLD) - ARRAY['settled_at','actual_input_tokens','actual_output_tokens','settled_cost_usd','cost_is_exact','usage'])
               OR NEW.settled_at IS NULL THEN
                RAISE EXCEPTION 'Model reservations allow one immutable usage settlement' USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$;
    """)
    op.execute(
        "CREATE TRIGGER protect_agent_model_reservation BEFORE INSERT OR UPDATE OR DELETE "
        "ON agent_model_reservations FOR EACH ROW EXECUTE FUNCTION protect_agent_model_reservation()"
    )


def downgrade():
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM agent_model_reservations) THEN
                RAISE EXCEPTION 'Cannot downgrade while model reservation history exists';
            END IF;
        END $$;
    """)
    op.drop_table("agent_model_reservations")
    op.execute("DROP FUNCTION protect_agent_model_reservation()")

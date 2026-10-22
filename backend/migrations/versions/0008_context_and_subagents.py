"""Immutable context checkpoints and durable, budgeted child task trees."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def replace_status(expression, name):
    checks = sa.inspect(op.get_bind()).get_check_constraints("agent_runs")
    for check in checks:
        if "status" in check["sqltext"]:
            op.drop_constraint(check["name"], "agent_runs", type_="check")
    op.create_check_constraint(name, "agent_runs", expression)


def upgrade():
    replace_status(
        "status IN ('queued','running','waiting_children','waiting_teacher','completed','failed','cancelled')",
        "ck_agent_run_status",
    )
    for name in ("parent_run_id", "root_run_id"):
        op.add_column("agent_runs", sa.Column(name, sa.Uuid(), nullable=True))
        op.create_foreign_key(f"fk_agent_{name}", "agent_runs", "agent_runs", [name], ["id"])
        op.create_index(f"ix_agent_runs_{name}", "agent_runs", [name])
    op.add_column("agent_runs", sa.Column("parent_tool_call_id", sa.String(120), nullable=True))
    for name in ("depth", "tree_models_used", "tree_tools_used", "children_spawned"):
        op.add_column(
            "agent_runs", sa.Column(name, sa.Integer(), server_default="0", nullable=False)
        )
    op.add_column(
        "agent_runs",
        sa.Column("resume_pending", sa.Boolean(), server_default="false", nullable=False),
    )
    op.execute("UPDATE agent_runs SET tree_models_used=models_used, tree_tools_used=tools_used")
    op.create_unique_constraint(
        "uq_agent_child_call", "agent_runs", ["parent_run_id", "parent_tool_call_id"]
    )
    op.create_check_constraint(
        "ck_agent_tree_shape",
        "agent_runs",
        "(parent_run_id IS NULL AND root_run_id IS NULL AND parent_tool_call_id IS NULL AND depth = 0) "
        "OR (parent_run_id IS NOT NULL AND root_run_id IS NOT NULL AND parent_tool_call_id IS NOT NULL "
        "AND depth > 0 AND parent_run_id <> id AND root_run_id <> id)",
    )
    op.create_check_constraint(
        "ck_agent_tree_budget",
        "agent_runs",
        "tree_models_used >= 0 AND tree_tools_used >= 0 AND children_spawned >= 0",
    )
    op.create_table(
        "agent_context_checkpoints",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("agent_runs.id"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("messages", JSONB(), nullable=False),
        sa.Column("details", JSONB(), nullable=False),
        sa.UniqueConstraint("run_id", "sequence"),
    )
    op.create_index("ix_agent_context_checkpoints_run_id", "agent_context_checkpoints", ["run_id"])
    op.execute(
        "CREATE TRIGGER protect_frozen_record BEFORE UPDATE OR DELETE ON agent_context_checkpoints "
        "FOR EACH ROW EXECUTE FUNCTION reject_frozen_mutation()"
    )
    op.execute("""
        CREATE FUNCTION protect_agent_tree() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.parent_run_id IS DISTINCT FROM OLD.parent_run_id
               OR NEW.root_run_id IS DISTINCT FROM OLD.root_run_id
               OR NEW.parent_tool_call_id IS DISTINCT FROM OLD.parent_tool_call_id
               OR NEW.depth IS DISTINCT FROM OLD.depth THEN
                RAISE EXCEPTION 'Harness task ancestry is immutable' USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END; $$;
    """)
    op.execute(
        "CREATE TRIGGER protect_agent_tree BEFORE UPDATE ON agent_runs "
        "FOR EACH ROW EXECUTE FUNCTION protect_agent_tree()"
    )


def downgrade():
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM agent_runs WHERE parent_run_id IS NOT NULL OR status='waiting_children')
               OR EXISTS (SELECT 1 FROM agent_context_checkpoints) THEN
                RAISE EXCEPTION 'Cannot downgrade while harness child tasks or context checkpoints exist';
            END IF;
        END $$;
    """)
    op.execute("DROP TRIGGER protect_agent_tree ON agent_runs")
    op.execute("DROP FUNCTION protect_agent_tree()")
    op.drop_table("agent_context_checkpoints")
    for name in ("ck_agent_tree_shape", "ck_agent_tree_budget"):
        op.drop_constraint(name, "agent_runs", type_="check")
    op.drop_constraint("uq_agent_child_call", "agent_runs", type_="unique")
    for name in ("parent_run_id", "root_run_id"):
        op.drop_index(f"ix_agent_runs_{name}", table_name="agent_runs")
        op.drop_constraint(f"fk_agent_{name}", "agent_runs", type_="foreignkey")
    for name in (
        "parent_run_id",
        "root_run_id",
        "parent_tool_call_id",
        "depth",
        "tree_models_used",
        "tree_tools_used",
        "children_spawned",
        "resume_pending",
    ):
        op.drop_column("agent_runs", name)
    replace_status(
        "status IN ('queued','running','waiting_teacher','completed','failed','cancelled')",
        "ck_agent_run_status",
    )

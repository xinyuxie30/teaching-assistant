"""Backfill only fingerprints observed on matching immutable legacy checkpoints."""

import importlib.util
from pathlib import Path
from uuid import UUID

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select
from teaching_assistant.db import ModelCall, Step

pytestmark = pytest.mark.postgres
MIGRATION_PATH = (
    Path(__file__).resolve().parents[1] / "migrations/versions/0004_provider_fingerprint.py"
)


async def test_backfill_preserves_unknowns_and_frozen_checkpoint_outputs(db, scenario):
    item_id = UUID(scenario["batch"]["item_ids"][0])
    observations = [
        ("point_a", "evaluate", {"provider_fingerprint": "legacy-assessment"}),
        (
            "point_a",
            "extract_evidence",
            {"model_metadata": {"provider_fingerprint": "legacy-evidence"}},
        ),
        ("point_b", "evaluate", {}),
        ("point_b", "extract_evidence", {"model_metadata": {"provider_fingerprint": 123}}),
    ]
    call_ids = []
    async with db.sessions.begin() as session:
        for criterion, kind, output in observations:
            call = ModelCall(
                item_id=item_id,
                epoch=1,
                criterion_id=criterion,
                kind=kind,
                input_hash="a" * 64,
                status="succeeded",
                requested_model="legacy-model",
            )
            session.add(call)
            await session.flush()
            call_ids.append(call.id)
            session.add(
                Step(
                    item_id=item_id,
                    criterion_id=criterion,
                    kind=kind,
                    call_id=call.id,
                    input_hash=call.input_hash,
                    output=output,
                )
            )
        for status in ("failed", "unknown"):
            call = ModelCall(
                item_id=item_id,
                epoch=1,
                criterion_id="point_a",
                kind="evaluate",
                input_hash="a" * 64,
                status=status,
                requested_model="legacy-model",
            )
            session.add(call)
            await session.flush()
            call_ids.append(call.id)

    spec = importlib.util.spec_from_file_location("fingerprint_migration", MIGRATION_PATH)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def replay_migration(connection):
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
            migration.upgrade()

    # The fixture has a private schema; only this fixture's receipts are changed.
    async with db.engine.begin() as connection:
        await connection.run_sync(replay_migration)
    async with db.sessions() as session:
        actual = [
            (await session.get(ModelCall, call_id)).provider_fingerprint for call_id in call_ids
        ]
        assert actual == ["legacy-assessment", "legacy-evidence", None, None, None, None]
        steps = list(await session.scalars(select(Step).where(Step.item_id == item_id)))
        assert {(step.criterion_id, step.kind): step.output for step in steps} == {
            (criterion, kind): output for criterion, kind, output in observations
        }

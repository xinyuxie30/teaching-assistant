"""Real PostgreSQL profile-registration races; no worker or provider calls."""

import asyncio
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.dml import Insert
from teaching_assistant.db import EvaluationProfile, GradingBatch, GradingItem, RubricVersion
from teaching_assistant.domain import DomainError, digest
from teaching_assistant.profiles import Profile, fake_profile
from teaching_assistant.schemas import BatchInput
from teaching_assistant.service import create_batch

pytestmark = pytest.mark.postgres


def new_profile(revision):
    manifest = {**fake_profile().manifest, "prompt_version": f"registration-fixture-{revision}"}
    return Profile(f"test-{digest(manifest)}", manifest)


async def owner_and_input(db, scenario, profile):
    async with db.sessions() as session:
        teacher = await session.scalar(
            select(RubricVersion.confirmed_by).where(
                RubricVersion.id == UUID(scenario["rubric"]["id"])
            )
        )
    return teacher, BatchInput.model_validate(
        {**scenario["batch_payload"], "evaluation_profile": profile.id}
    )


async def database_counts(db):
    async with db.sessions() as session:
        return tuple(
            [
                await session.scalar(select(func.count()).select_from(model))
                for model in (EvaluationProfile, GradingBatch, GradingItem)
            ]
        )


async def test_concurrent_first_profile_registration_reuses_one_immutable_profile(db, scenario):
    # Eight held transactions fit the fixture pool; the gate is immediately before
    # the first profile INSERT, after owned_rubric has acquired a real connection.
    concurrent = 8
    for revision in range(3):
        profile = new_profile(revision)
        teacher, data = await owner_and_input(db, scenario, profile)
        before = await database_counts(db)
        barrier = asyncio.Barrier(concurrent)

        class SynchronizedSession(AsyncSession):
            registration_barrier = barrier
            arrivals = 0

            async def execute(self, statement, *args, **kwargs):
                if isinstance(statement, Insert) and statement.table.name == "evaluation_profiles":
                    assert not self.info.get("profile_insert_seen")
                    self.info["profile_insert_seen"] = True
                    type(self).arrivals += 1
                    await self.registration_barrier.wait()
                return await super().execute(statement, *args, **kwargs)

        sessions = async_sessionmaker(db.engine, class_=SynchronizedSession, expire_on_commit=False)

        async def submit(sessions, teacher, data, profile):
            async with sessions.begin() as session:
                return await create_batch(session, teacher, data, profile=profile)

        outcomes = await asyncio.wait_for(
            asyncio.gather(
                *(submit(sessions, teacher, data, profile) for _ in range(concurrent)),
                return_exceptions=True,
            ),
            timeout=30,
        )
        assert SynchronizedSession.arrivals == concurrent
        assert all(isinstance(outcome, dict) for outcome in outcomes), outcomes
        assert len({outcome["id"] for outcome in outcomes}) == concurrent
        assert len({tuple(outcome["item_ids"]) for outcome in outcomes}) == 1
        assert all(outcome["evaluation_profile"] == profile.id for outcome in outcomes)
        assert len(outcomes[0]["item_ids"]) == len(data.answer_version_ids)
        after = await database_counts(db)
        assert after == (
            before[0] + 1,
            before[1] + concurrent,
            before[2] + len(data.answer_version_ids),
        )
        async with db.sessions() as session:
            stored = await session.get(EvaluationProfile, profile.id)
            assert stored.manifest == profile.manifest
            assert stored.fingerprint == profile.fingerprint
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(EvaluationProfile)
                    .where(EvaluationProfile.fingerprint == profile.fingerprint)
                )
                == 1
            )


@pytest.mark.parametrize("conflict", ["same_id_changed_manifest", "different_id_same_fingerprint"])
async def test_profile_conflicts_are_explicit_and_leave_no_partial_batch(db, scenario, conflict):
    original = new_profile("conflict")
    teacher, data = await owner_and_input(db, scenario, original)
    async with db.sessions.begin() as session:
        await create_batch(session, teacher, data, profile=original)
    if conflict == "same_id_changed_manifest":
        requested = Profile(original.id, {**original.manifest, "prompt_version": "changed"})
    else:
        requested = Profile("alias-" + original.fingerprint, dict(original.manifest))
    before = await database_counts(db)
    requested_data = data.model_copy(update={"evaluation_profile": requested.id})
    with pytest.raises(DomainError) as raised:
        async with db.sessions.begin() as session:
            await create_batch(session, teacher, requested_data, profile=requested)
    assert raised.value.code == "evaluation_profile_conflict"
    assert raised.value.status == 409
    assert await database_counts(db) == before
    async with db.sessions() as session:
        stored = await session.get(EvaluationProfile, original.id)
        assert stored.manifest == original.manifest
        assert stored.fingerprint == original.fingerprint
        if requested.id != original.id:
            assert await session.get(EvaluationProfile, requested.id) is None

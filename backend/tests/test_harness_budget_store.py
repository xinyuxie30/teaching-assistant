"""Shared reservations, exact reported usage and immutable accounting in PostgreSQL."""

import asyncio
from copy import deepcopy
from decimal import Decimal
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from conftest import post
from sqlalchemy import delete, select
from sqlalchemy.exc import DBAPIError
from teaching_assistant.db import AgentModelReservation, AgentRun
from teaching_assistant.domain import ModelError, digest
from teaching_assistant.harness import budgets, scheduler, store

pytestmark = pytest.mark.postgres

BODY = {
    "model": "fixture",
    "messages": [{"role": "user", "content": "fixed facts"}],
    "max_tokens": 100,
}
POLICY = budgets.BudgetPolicy(
    context_window_tokens=100000,
    context_source="fixture",
    price_version="fixture-v1",
    price_source="fixture",
    input_usd_per_million="1",
    output_usd_per_million="2",
    cached_input_usd_per_million="0.1",
).model_dump(mode="json")
USAGE = {
    "input_tokens": 120,
    "output_tokens": 25,
    "input_cached_tokens": 100,
    "input_uncached_tokens": 20,
}


@pytest.fixture
async def budget_run(db, client, scenario):
    created = await post(
        client, f"/v1/question-versions/{scenario['question']['id']}/rubric-agent-runs", {}
    )
    run_id = UUID(created["id"])
    async with db.sessions.begin() as session:
        run = await session.get(AgentRun, run_id)
        run.manifest = {**run.manifest, "budget": deepcopy(POLICY), "max_output_tokens": 100}
    return run_id


async def dispatch(db, run_id):
    async with db.sessions.begin() as session:
        _, run = await scheduler.lock_run(session, run_id)
        sequence = store.event(session, run, "model_dispatched", prompt_hash=digest(BODY))
        row = await budgets.reserve(session, run, sequence, BODY)
        return sequence, row.id


async def totals(db, run_id):
    async with db.sessions() as session:
        return await budgets.report(session, await session.get(AgentRun, run_id))


async def test_unknown_and_incomplete_usage_keep_full_reservation_on_restart(db, budget_run):
    sequence, reservation_id = await dispatch(db, budget_run)
    before = await totals(db, budget_run)
    assert before["reserved_tokens"] > 100 and before["reported_tokens"] == 0
    assert before["unsettled_calls"] == 1
    async with db.sessions.begin() as session:
        _, run = await scheduler.lock_run(session, budget_run)
        for unknown in (
            None,
            {},
            {"input_tokens": 100},
            {"input_tokens": 100, "output_tokens": 20, "input_cached_tokens": 100},
        ):
            assert await budgets.settle(session, run, sequence, unknown) is None
        duplicate = await budgets.reserve(session, run, sequence, BODY)
        assert duplicate.id == reservation_id
    assert await totals(db, budget_run) == before


async def test_reported_cache_usage_settles_once_and_releases_unused_capacity(db, budget_run):
    sequence, _ = await dispatch(db, budget_run)
    async with db.sessions.begin() as session:
        _, run = await scheduler.lock_run(session, budget_run)
        assert await budgets.settle(session, run, sequence, USAGE) is None
        assert await budgets.settle(session, run, sequence, USAGE) is None
    result = await totals(db, budget_run)
    assert result["reported_tokens"] == result["accounted_tokens"] == 145
    assert result["reserved_tokens"] == result["unsettled_calls"] == 0
    assert Decimal(result["known_cost_usd"]) == Decimal("0.000080")
    assert result["known_cost_usd"] == result["accounted_cost_usd"]
    async with db.sessions.begin() as session:
        _, run = await scheduler.lock_run(session, budget_run)
        with pytest.raises(ModelError) as failure:
            await budgets.settle(session, run, sequence, {**USAGE, "output_tokens": 26})
        assert failure.value.code == "agent_usage_receipt_conflict"


async def test_missing_cache_breakdown_keeps_conservative_cost_but_exact_tokens(db, budget_run):
    sequence, _ = await dispatch(db, budget_run)
    async with db.sessions.begin() as session:
        _, run = await scheduler.lock_run(session, budget_run)
        await budgets.settle(session, run, sequence, {"input_tokens": 120, "output_tokens": 25})
    result = await totals(db, budget_run)
    assert result["reported_tokens"] == 145
    assert Decimal(result["known_cost_usd"]) == 0
    assert Decimal(result["accounted_cost_usd"]) == Decimal("0.000170")
    async with db.sessions() as session:
        row = await session.scalar(select(AgentModelReservation))
        assert row.cost_is_exact is False


@pytest.mark.parametrize("resource", ["tokens", "cost"])
async def test_parallel_children_cannot_over_reserve_the_shared_root(db, budget_run, resource):
    async with db.sessions.begin() as session:
        root = await session.get(AgentRun, budget_run)
        bounds = budgets.request_bounds(BODY, root.manifest)
        root.limits = {
            **root.limits,
            **(
                {"max_total_tokens": bounds["total_tokens"] + 1}
                if resource == "tokens"
                else {"max_cost_usd": str(bounds["cost_usd"] * Decimal("1.5"))}
            ),
        }
        children = []
        for index in range(2):
            child = AgentRun(
                teacher_id=root.teacher_id,
                question_version_id=root.question_version_id,
                task_type=root.task_type,
                task_input={},
                parent_run_id=root.id,
                root_run_id=root.id,
                parent_tool_call_id=f"fixture-{index}",
                depth=1,
                profile_id=root.profile_id,
                manifest=deepcopy(root.manifest),
                limits=deepcopy(root.limits),
                messages=[],
                expires_at=root.expires_at,
            )
            session.add(child)
            children.append(child)
        await session.flush()
        ids = [child.id for child in children]

    async def attempt(child_id):
        try:
            await dispatch(db, child_id)
            return "reserved"
        except ModelError as exc:
            return exc.code

    outcomes = await asyncio.gather(*(attempt(child_id) for child_id in ids))
    assert outcomes.count("reserved") == 1
    assert (
        outcomes.count(
            "agent_token_budget_exhausted"
            if resource == "tokens"
            else "agent_cost_budget_exhausted"
        )
        == 1
    )
    result = await totals(db, budget_run)
    assert result["calls"] == 1 and result["accounted_tokens"] == bounds["total_tokens"]


async def test_known_overrun_is_recorded_before_returning_budget_violation(db, budget_run):
    async with db.sessions.begin() as session:
        run = await session.get(AgentRun, budget_run)
        bound = budgets.request_bounds(BODY, run.manifest)
        run.limits = {**run.limits, "max_total_tokens": bound["total_tokens"]}
    sequence, _ = await dispatch(db, budget_run)
    actual = {"input_tokens": bound["input_tokens"] + 10, "output_tokens": bound["output_tokens"]}
    async with db.sessions.begin() as session:
        _, run = await scheduler.lock_run(session, budget_run)
        assert (
            await budgets.settle(session, run, sequence, actual) == "agent_token_budget_exhausted"
        )
    result = await totals(db, budget_run)
    assert result["reported_tokens"] == bound["total_tokens"] + 10
    assert result["unsettled_calls"] == 0
    with pytest.raises(ModelError) as failure:
        await dispatch(db, budget_run)
    assert failure.value.code == "agent_token_budget_exhausted"


async def test_unknown_calls_reserve_capacity_until_known_usage_is_settled(db, budget_run):
    async with db.sessions.begin() as session:
        run = await session.get(AgentRun, budget_run)
        bounds = budgets.request_bounds(BODY, run.manifest)
        run.limits = {**run.limits, "max_total_tokens": bounds["total_tokens"] + 200}
    sequence, _ = await dispatch(db, budget_run)
    with pytest.raises(ModelError):
        await dispatch(db, budget_run)
    async with db.sessions.begin() as session:
        _, run = await scheduler.lock_run(session, budget_run)
        await budgets.settle(session, run, sequence, USAGE)
    await dispatch(db, budget_run)
    result = await totals(db, budget_run)
    assert result["calls"] == 2 and result["unsettled_calls"] == 1
    assert result["accounted_tokens"] == bounds["total_tokens"] + 145


@pytest.mark.parametrize("mutation", ["reserved", "policy", "settled", "delete"])
async def test_database_protects_request_and_settled_history(db, budget_run, mutation):
    sequence, reservation_id = await dispatch(db, budget_run)
    if mutation == "settled":
        async with db.sessions.begin() as session:
            _, run = await scheduler.lock_run(session, budget_run)
            await budgets.settle(session, run, sequence, USAGE)
    with pytest.raises(DBAPIError):
        async with db.sessions.begin() as session:
            row = await session.get(AgentModelReservation, reservation_id)
            if mutation == "reserved":
                row.reserved_input_tokens += 1
            elif mutation == "policy":
                row.policy = {**row.policy, "price_version": "changed"}
            elif mutation == "settled":
                row.actual_output_tokens += 1
            else:
                await session.execute(
                    delete(AgentModelReservation).where(AgentModelReservation.id == reservation_id)
                )
            await session.flush()


async def test_empty_budget_migration_round_trip_and_orm_match(db):
    def round_trip(connection):
        config = Config("alembic.ini")
        config.attributes["connection"] = connection
        command.downgrade(config, "0011")
        command.upgrade(config, "head")
        command.check(config)

    async with db.engine.begin() as connection:
        await connection.run_sync(round_trip)


async def test_budget_migration_cannot_discard_unknown_reservations(db, budget_run):
    await dispatch(db, budget_run)

    def downgrade(connection):
        config = Config("alembic.ini")
        config.attributes["connection"] = connection
        command.downgrade(config, "0011")

    with pytest.raises(DBAPIError):
        async with db.engine.begin() as connection:
            await connection.run_sync(downgrade)
    assert (await totals(db, budget_run))["calls"] == 1


async def test_legacy_runs_do_not_create_budget_rows(db, budget_run):
    async with db.sessions.begin() as session:
        _, run = await scheduler.lock_run(session, budget_run)
        run.manifest = {name: value for name, value in run.manifest.items() if name != "budget"}
        assert await budgets.reserve(session, run, 99, BODY) is None
        assert await budgets.settle(session, run, 99, USAGE) is None
    assert (await totals(db, budget_run))["calls"] == 0

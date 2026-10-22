"""Worker dispatch, provider failures and tree cancellation preserve budget receipts."""

import json
from decimal import Decimal
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select
from teaching_assistant.db import AgentEvent, AgentModelReservation, AgentRun
from teaching_assistant.domain import ModelError
from teaching_assistant.harness import budgets
from teaching_assistant.harness import worker as worker_module
from teaching_assistant.harness.models import DeepSeekAgentModel
from teaching_assistant.harness.rubric_model import FakeAgentModel
from teaching_assistant.harness.worker import HarnessWorker
from test_harness_core import start
from test_harness_parallel import nested, rows
from test_harness_parallel import parallel_course as parallel_course  # noqa: F401

pytestmark = pytest.mark.postgres

USAGE = {
    "input_tokens": 120,
    "output_tokens": 25,
    "input_cached_tokens": 100,
    "input_uncached_tokens": 20,
}


@pytest.fixture
async def provider(settings, monkeypatch):
    # Only MockTransport sees this endpoint and fixture credential; no paid model call.
    settings.deepseek_model = settings.deepseek_expected_model = "budget-fixture-model"
    settings.deepseek_api_key = SecretStr("budget-fixture-key")
    settings.deepseek_endpoint = "https://budget-provider.invalid/chat/completions"
    settings.deepseek_input_usd_per_million = Decimal("1")
    settings.deepseek_output_usd_per_million = Decimal("2")
    settings.deepseek_cached_input_usd_per_million = Decimal("0.1")
    settings.deepseek_price_version = "budget-worker-fixture-v1"
    settings.deepseek_price_source = "test fixture"
    calls, control = [], {"mode": "normal"}

    async def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        if control["mode"] == "timeout":
            raise httpx.ReadTimeout("Unknown mock provider outcome", request=request)
        reply = await FakeAgentModel().respond(body, {"model": settings.deepseek_model})
        return httpx.Response(
            200,
            headers={"x-request-id": f"budget-request-{len(calls)}"},
            json={
                "model": "unexpected-model"
                if control["mode"] == "mismatch"
                else settings.deepseek_model,
                "system_fingerprint": "budget-fixture-fingerprint",
                "usage": {
                    "prompt_tokens": 120,
                    "completion_tokens": 25,
                    "total_tokens": 145,
                    "prompt_cache_hit_tokens": 100,
                    "prompt_cache_miss_tokens": 20,
                },
                "choices": [{"finish_reason": "tool_calls", "message": reply.message()}],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
        monkeypatch.setattr(
            worker_module,
            "build_model",
            lambda config, policy: DeepSeekAgentModel(config, client=transport),
        )
        yield calls, control


async def accounting(db, run_id):
    async with db.sessions() as session:
        run = await session.get(AgentRun, run_id)
        reservations = list(
            await session.scalars(
                select(AgentModelReservation)
                .where(AgentModelReservation.root_run_id == (run.root_run_id or run.id))
                .order_by(AgentModelReservation.created_at)
            )
        )
        events = list(
            await session.scalars(
                select(AgentEvent).where(AgentEvent.run_id == run_id).order_by(AgentEvent.sequence)
            )
        )
        root = await session.get(AgentRun, run.root_run_id or run.id)
        return run, reservations, events, await budgets.report(session, root)


async def test_normal_provider_usage_settles_each_worker_dispatch(
    db, settings, client, scenario, provider
):
    calls, _ = provider
    created = await start(
        client,
        scenario,
        agent_profile="deepseek-rubric-agent-v1",
        max_total_tokens=100000,
        max_cost_usd="1",
    )
    run_id = UUID(created["id"])
    assert await HarnessWorker(db, settings, run_ids=(run_id,)).run_once()
    run, reservations, events, totals = await accounting(db, run_id)
    assert run.status == "waiting_teacher" and run.draft_id is not None
    assert len(calls) == run.models_used == run.tree_models_used == len(reservations) == 2
    assert run.tools_used == 2 and run.inflight_seq is None
    dispatches = {event.sequence for event in events if event.kind == "model_dispatched"}
    assert {row.dispatch_sequence for row in reservations} == dispatches
    assert all(row.usage == USAGE and row.settled_at is not None for row in reservations)
    assert all(row.cost_is_exact for row in reservations)
    assert totals["reported_tokens"] == totals["accounted_tokens"] == 290
    assert totals["reserved_tokens"] == totals["unsettled_calls"] == 0
    assert Decimal(totals["known_cost_usd"]) == Decimal("0.000160")


async def test_returned_model_mismatch_still_settles_known_usage(
    db, settings, client, scenario, provider
):
    calls, control = provider
    control["mode"] = "mismatch"
    created = await start(client, scenario, agent_profile="deepseek-rubric-agent-v1")
    run_id = UUID(created["id"])
    assert await HarnessWorker(db, settings, run_ids=(run_id,)).run_once()
    run, reservations, events, totals = await accounting(db, run_id)
    assert run.status == "failed" and run.last_error == "provider_model_version_mismatch"
    assert len(calls) == len(reservations) == run.models_used == 1
    assert run.tools_used == 0 and run.draft_id is None and run.inflight_seq is None
    assert reservations[0].usage == USAGE and reservations[0].settled_at is not None
    assert totals["reported_tokens"] == 145 and totals["unsettled_calls"] == 0
    assert Decimal(totals["known_cost_usd"]) == Decimal("0.000080")
    failures = [event for event in events if event.kind == "model_failed"]
    assert len(failures) == 1
    receipt = failures[0].body
    assert receipt["usage"] == USAGE and receipt["returned_model"] == "unexpected-model"
    assert receipt["provider_request_id"] == "budget-request-1"
    assert receipt["provider_fingerprint"] == "budget-fixture-fingerprint"
    assert receipt["http_status"] == 200
    assert not any(event.kind in {"model_succeeded", "tools_requested"} for event in events)


@pytest.mark.parametrize(
    ("limits", "error_code"),
    [
        ({"max_total_tokens": 1}, "agent_token_budget_exhausted"),
        ({"max_cost_usd": "0.00000001"}, "agent_cost_budget_exhausted"),
    ],
)
async def test_reservation_rejection_rolls_back_dispatch_and_counters_before_invoke(
    db, settings, client, scenario, provider, monkeypatch, limits, error_code
):
    calls, _ = provider
    created = await start(client, scenario, agent_profile="deepseek-rubric-agent-v1", **limits)
    run_id = UUID(created["id"])

    async def forbidden_invoke(*args):
        pytest.fail("A rejected reservation must never invoke a model")

    monkeypatch.setattr(HarnessWorker, "invoke", forbidden_invoke)
    assert await HarnessWorker(db, settings, run_ids=(run_id,)).run_once()
    run, reservations, events, totals = await accounting(db, run_id)
    assert run.status == "failed" and run.last_error == error_code
    assert run.models_used == run.tree_models_used == run.tools_used == 0
    assert run.inflight_seq is None and calls == reservations == []
    assert totals["calls"] == totals["accounted_tokens"] == 0
    assert not any(event.kind.startswith("model_") for event in events)


async def test_unknown_timeout_keeps_reservations_across_worker_retry(
    db, settings, client, scenario, provider
):
    calls, control = provider
    control["mode"] = "timeout"
    created = await start(
        client,
        scenario,
        agent_profile="deepseek-rubric-agent-v1",
        max_model_calls=2,
        max_total_tokens=100000,
        max_cost_usd="1",
    )
    run_id = UUID(created["id"])
    assert await HarnessWorker(db, settings, run_ids=(run_id,)).run_once()
    first, before, _, first_totals = await accounting(db, run_id)
    assert first.status == "queued" and first.last_error == "provider_timeout"
    assert first.models_used == first.tree_models_used == len(before) == len(calls) == 1
    assert before[0].settled_at is None and before[0].usage is None
    assert first_totals["accounted_tokens"] == first_totals["reserved_tokens"] > 0
    assert Decimal(first_totals["reserved_cost_usd"]) > 0
    assert await HarnessWorker(db, settings, run_ids=(run_id,)).run_once()
    run, after, events, totals = await accounting(db, run_id)
    assert run.status == "failed" and run.last_error == "provider_timeout"
    assert run.models_used == run.tree_models_used == len(calls) == len(after) == 2
    assert before[0].id == after[0].id
    assert all(row.settled_at is None and row.usage is None for row in after)
    assert totals["unsettled_calls"] == 2 and totals["reported_tokens"] == 0
    assert totals["accounted_tokens"] == 2 * first_totals["accounted_tokens"]
    assert Decimal(totals["reserved_cost_usd"]) == 2 * Decimal(first_totals["reserved_cost_usd"])
    assert sum(event.kind == "model_dispatched" for event in events) == 2
    assert sum(event.kind == "model_unknown" for event in events) == 2
    assert run.tools_used == 0 and run.inflight_seq is None


@pytest.mark.parametrize("provider_failure", [False, True])
async def test_known_overrun_preserves_actual_usage_and_cancels_the_tree(
    db, settings, client, parallel_course, monkeypatch, provider_failure
):
    created, _, _, leaf = await nested(
        db, settings, client, parallel_course, max_total_tokens=200000
    )
    root_id = UUID(created["id"])
    _, prior, _, before = await accounting(db, root_id)
    original_factory = worker_module.build_model
    calls = []

    class OverspendingModel:
        def __init__(self, delegate):
            self.delegate = delegate

        async def respond(self, body, policy):
            calls.append(body)
            usage = {
                "input_tokens": 200001 if provider_failure else 120,
                "output_tokens": body["max_tokens"] + 1,
            }
            if provider_failure:
                raise ModelError(
                    "provider_model_version_mismatch",
                    retryable=False,
                    usage=usage,
                    returned_model="unexpected-overrun-model",
                    provider_request_id="overrun-request",
                    provider_fingerprint="overrun-fingerprint",
                    http_status=200,
                )
            reply = await self.delegate.respond(body, policy)
            reply.metadata = reply.metadata.model_copy(
                update={
                    "usage": usage,
                    "provider_request_id": "overrun-request",
                    "provider_fingerprint": "overrun-fingerprint",
                    "http_status": 200,
                }
            )
            return reply

    monkeypatch.setattr(
        worker_module,
        "build_model",
        lambda config, policy: OverspendingModel(original_factory(config, policy)),
    )
    assert await HarnessWorker(db, settings, run_ids=(leaf.id,)).run_once()
    child, reservations, events, totals = await accounting(db, leaf.id)
    tree = await rows(db, root_id)
    expected = (
        "agent_token_budget_exhausted" if provider_failure else "agent_usage_exceeds_reservation"
    )
    assert tree[0].status == "failed" and tree[0].last_error == expected
    assert all(run.status == "cancelled" and run.last_error == expected for run in tree[1:])
    assert len(calls) == child.models_used == 1 and child.inflight_seq is None
    assert child.tools_used == 0
    assert all(run.pending_calls == [] for run in tree)
    assert len(reservations) == len(prior) + 1
    assert {row.id for row in prior} <= {row.id for row in reservations}
    settled = [row for row in reservations if row.settled_at is not None]
    assert len(settled) == 1 and settled[0].run_id == leaf.id
    actual = settled[0]
    assert actual.actual_output_tokens == actual.reserved_output_tokens + 1
    assert actual.actual_input_tokens == (200001 if provider_failure else 120)
    assert totals["reported_tokens"] == actual.actual_input_tokens + actual.actual_output_tokens
    assert totals["reserved_tokens"] == before["reserved_tokens"]
    assert totals["unsettled_calls"] == len(prior)
    receipts = [event for event in events if event.kind in {"model_succeeded", "model_failed"}]
    assert len(receipts) == 1
    receipt = receipts[0].body if provider_failure else receipts[0].body["metadata"]
    assert receipt["usage"] == actual.usage
    assert receipt["provider_request_id"] == "overrun-request"
    assert receipt["provider_fingerprint"] == "overrun-fingerprint"
    assert receipt["http_status"] == 200
    if provider_failure:
        assert receipt["returned_model"] == "unexpected-overrun-model"
        assert receipt["error_code"] == expected
    assert not await HarnessWorker(db, settings, run_ids=(root_id,)).run_once()
    assert len(calls) == 1

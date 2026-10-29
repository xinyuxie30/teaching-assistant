"""Real Harness -> grading tool -> Jev adapter -> committed PostgreSQL results."""

import asyncio
import json
from datetime import timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from conftest import post
from sqlalchemy import func, select
from teaching_assistant import service as grading_service
from teaching_assistant.api import create_app
from teaching_assistant.cli import bootstrap
from teaching_assistant.db import (
    AgentArtifact,
    AgentRun,
    Assessment,
    GradingBatch,
    GradingItem,
    ModelCall,
)
from teaching_assistant.domain import DomainError
from teaching_assistant.evaluators import ManualEvidenceExtractor
from teaching_assistant.harness.worker import HarnessWorker
from teaching_assistant.jev import JevEvaluator
from teaching_assistant.profiles import configured_jev_profile
from teaching_assistant.worker import Worker
from test_jev import response_body

pytestmark = pytest.mark.postgres


@pytest.fixture
async def grading_client(db, settings, scenario):
    config = settings.model_copy(
        update={
            "jev_model": "jev-1.14.0",
            "jev_api_key": "not-a-real-key",
        }
    )
    # Preserve SecretStr validation when replacing fixture credentials.
    config = type(settings).model_validate(config.model_dump())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(config, db)),
        base_url="http://test",
        headers={"Authorization": f"Bearer {config.dev_api_token}"},
    ) as client:
        yield client, config


async def start(client, scenario, **options):
    return await post(
        client,
        "/v1/grading-agent-runs",
        {
            "rubric_version_id": scenario["rubric"]["id"],
            "answer_version_ids": scenario["imported"]["answer_version_ids"],
            **options,
        },
        key=options.get("instructions", "grading-fixture"),
    )


async def make_ready(db, run_id):
    async with db.sessions.begin() as session:
        run = await session.get(AgentRun, UUID(run_id))
        run.ready_at = await session.scalar(select(func.clock_timestamp()))


@pytest.mark.parametrize("fault", [None, "rate_limit", "invalid", "auth", "tie", "timeout"])
async def test_full_tool_chain_waits_for_commits_and_reports_failures(
    db,
    scenario,
    grading_client,
    monkeypatch,
    fault,
):
    client, config = grading_client
    seen = 0

    def provider(request):
        nonlocal seen
        seen += 1
        if seen == 1:
            if fault in {"rate_limit", "auth"}:
                return httpx.Response(
                    429 if fault == "rate_limit" else 401, headers={"Retry-After": "0"}
                )
            if fault == "invalid":
                return httpx.Response(200, json={})
            if fault == "timeout":
                raise httpx.ReadTimeout("fixture", request=request)
        body = response_body(json.loads(request.content))
        if fault == "tie":
            size = len(body["answers"]["criterion"]["legend"])
            body["answers"]["criterion"].update(
                probabilities={str(i): 1 / size for i in range(size)},
                score=(size - 1) / 2,
            )
        return httpx.Response(200, json=body, headers={"x-typesafe-request-id": f"fixture-{seen}"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as provider_client:
        monkeypatch.setattr(
            "teaching_assistant.worker.build_adapters",
            lambda settings, profile: (
                JevEvaluator(settings, profile, client=provider_client),
                ManualEvidenceExtractor(),
            ),
        )
        run = await start(client, scenario)
        assert (await start(client, scenario))["id"] == run["id"]
        harness = HarnessWorker(db, config, run_ids=(UUID(run["id"]),))
        await harness.run_once()
        saved = (await client.get(f"/v1/agent-runs/{run['id']}")).json()
        assert saved["status"] == "queued" and saved["grading_batch_id"]
        assert saved["models_used"] == saved["tools_used"] == 2
        assert (await client.get(f"/v1/agent-runs/{run['id']}/artifact")).status_code == 404
        # Restarting and polling before grading cannot declare success or consume attempts.
        await make_ready(db, run["id"])
        await HarnessWorker(db, config, run_ids=(UUID(run["id"]),)).run_once()
        polled = (await client.get(f"/v1/agent-runs/{run['id']}")).json()
        assert polled["models_used"] == saved["models_used"] and polled["attempts"] == 1
        batch = (await client.get(f"/v1/grading-batches/{saved['grading_batch_id']}")).json()
        ids = tuple(UUID(row["id"]) for row in batch["items"])
        workers = [Worker(db, config, item_ids=ids) for _ in range(3)]

        async def drain(worker):
            while await worker.run_once():
                pass

        await asyncio.gather(*(drain(worker) for worker in workers))
        await make_ready(db, run["id"])
        await HarnessWorker(db, config, run_ids=(UUID(run["id"]),)).run_once()
        detail = (await client.get(f"/v1/agent-runs/{run['id']}")).json()
        assert detail["status"] == "completed"
        assert detail["models_used"] == detail["tools_used"] == 3
        artifact = (await client.get(f"/v1/agent-runs/{run['id']}/artifact")).json()
        body = artifact["body"]
        assert body["total"] == 2 and body["teacher_review_required"] is True
        assert body["failed"] == (1 if fault == "auth" else 0)
        assert body["abstained"] == (2 if fault == "tie" else 0)
        assert body["outcome"] == ("partial_failure" if fault == "auth" else "all_succeeded")
        assert artifact["confirmed_at"] is None
        # Completing an execution must not offer a shortcut around per-item teacher reviews.
        await post(
            client,
            f"/v1/agent-runs/{run['id']}/confirm",
            {"content_hash": artifact["content_hash"]},
            expected=409,
        )
        async with db.sessions() as session:
            items = list(await session.scalars(select(GradingItem).where(GradingItem.id.in_(ids))))
            assert all(item.review_revision == 0 for item in items)
            assert all(item.lease_owner is item.lease_until is None for item in items)
            if fault == "tie":
                assert all(item.suggested_score is None for item in items)
            assessments = list(
                await session.scalars(select(Assessment).where(Assessment.item_id.in_(ids)))
            )
            assert len(assessments) == body["succeeded"] * 2
            calls = list(await session.scalars(select(ModelCall).where(ModelCall.item_id.in_(ids))))
            assert sum(item.calls_used for item in items) == len(calls)
            assert not any(call.status == "dispatched" for call in calls)
            if fault in {"rate_limit", "invalid", "timeout"}:
                assert seen == 5  # Four successful Jev requests plus exactly one retry.
            assert await session.scalar(select(func.count()).select_from(AgentArtifact)) == 1


async def test_repeated_runs_reuse_grading_targets_and_do_not_repeat_jev_calls(
    db,
    scenario,
    grading_client,
    monkeypatch,
):
    client, config = grading_client
    requests = []

    def provider(request):
        requests.append(request)
        return httpx.Response(200, json=response_body(json.loads(request.content)))

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as provider_client:
        monkeypatch.setattr(
            "teaching_assistant.worker.build_adapters",
            lambda settings, profile: (
                JevEvaluator(settings, profile, client=provider_client),
                ManualEvidenceExtractor(),
            ),
        )
        runs = [await start(client, scenario, instructions=f"fixed-task-{i}") for i in range(2)]
        for run in runs:
            await HarnessWorker(db, config, run_ids=(UUID(run["id"]),)).run_once()
        profile = configured_jev_profile(config)
        async with db.sessions() as session:
            ids = tuple(
                await session.scalars(
                    select(GradingItem.id).where(GradingItem.evaluation_profile == profile.id)
                )
            )
        assert len(ids) == 2
        worker = Worker(db, config, item_ids=ids)
        while await worker.run_once():
            pass
        for run in runs:
            await make_ready(db, run["id"])
            await HarnessWorker(db, config, run_ids=(UUID(run["id"]),)).run_once()
        assert len(requests) == 4


async def test_grading_scope_profiles_and_permissions(db, scenario, grading_client, settings):
    client, config = grading_client
    await post(
        client,
        "/v1/grading-agent-runs",
        {
            "rubric_version_id": scenario["rubric"]["id"],
            "answer_version_ids": scenario["imported"]["answer_version_ids"],
            "evaluation_profile": "fake-v1",
        },
        expected=422,
    )
    other = config.model_copy(update={"dev_api_token": "other-" + uuid4().hex})
    await bootstrap(db, other)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(other, db)),
        base_url="http://test",
        headers={"Authorization": f"Bearer {other.dev_api_token}"},
    ) as outsider:
        await post(
            outsider,
            "/v1/grading-agent-runs",
            {
                "rubric_version_id": scenario["rubric"]["id"],
                "answer_version_ids": scenario["imported"]["answer_version_ids"],
            },
            expected=404,
        )
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AgentRun)) == 0


async def test_waiting_for_grading_keeps_deadline_and_does_not_create_duplicate_batch(
    db,
    scenario,
    grading_client,
):
    client, config = grading_client
    run = await start(client, scenario)
    harness = HarnessWorker(db, config, run_ids=(UUID(run["id"]),))
    await harness.run_once()
    async with db.sessions.begin() as session:
        saved = await session.get(AgentRun, UUID(run["id"]))
        saved.expires_at = await session.scalar(select(func.clock_timestamp())) - timedelta(
            seconds=1
        )
    await harness.run_once()
    detail = (await client.get(f"/v1/agent-runs/{run['id']}")).json()
    assert detail["status"] == "failed" and detail["error_code"] == "agent_deadline_exceeded"
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AgentArtifact)) == 0
        # The preexisting scenario's fake batch plus one Jev batch.
        assert await session.scalar(select(func.count()).select_from(GradingBatch)) == 2


async def test_grading_tool_rolls_back_uncommitted_effect_before_retry(
    db,
    scenario,
    grading_client,
    monkeypatch,
):
    client, config = grading_client
    original = grading_service.create_batch
    created_ids = []

    async def interrupted_batch(*args, **kwargs):
        result = await original(*args, **kwargs)
        created_ids.append(UUID(result["id"]))
        if len(created_ids) == 1:
            raise DomainError("injected_before_commit", "Synthetic transaction failure")
        return result

    monkeypatch.setattr(grading_service, "create_batch", interrupted_batch)
    run = await start(client, scenario)
    await HarnessWorker(db, config, run_ids=(UUID(run["id"]),)).run_once()
    assert len(created_ids) == 2
    async with db.sessions() as session:
        assert await session.get(GradingBatch, created_ids[0]) is None
        assert await session.get(GradingBatch, created_ids[1]) is not None
        saved = await session.get(AgentRun, UUID(run["id"]))
        assert saved.task_state["batch_id"] == str(created_ids[1])
        assert saved.tools_used == 3  # Read, rejected write, committed write.
        assert saved.status == "queued"
        replies = [
            json.loads(message["content"])
            for message in saved.messages
            if message.get("role") == "tool"
        ]
        assert replies[1]["error_code"] == "injected_before_commit"
        assert replies[2]["result"]["batch_id"] == str(created_ids[1])

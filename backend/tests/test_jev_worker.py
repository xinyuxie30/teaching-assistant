"""Exercise the real adapter's error path with an in-process provider, never paid calls."""

import json
from uuid import UUID

import httpx
import pytest
from conftest import post
from teaching_assistant.api import create_app
from teaching_assistant.evaluators import ManualEvidenceExtractor
from teaching_assistant.jev import JevEvaluator
from teaching_assistant.profiles import configured_jev_profile
from teaching_assistant.worker import Worker
from test_jev import response_body
from test_jev import settings as jev_settings

pytestmark = pytest.mark.postgres


async def test_rate_limit_is_durable_then_resumed_without_losing_provider_receipt(
    db, settings, scenario, monkeypatch
):
    config = jev_settings(dev_api_token=settings.dev_api_token, retry_delay_seconds=0)
    profile = configured_jev_profile(config)
    calls = []

    def provider(request):
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return httpx.Response(
                429, headers={"Retry-After": "0", "x-typesafe-request-id": "limited-attempt"}
            )
        return httpx.Response(
            200,
            json=response_body(calls[-1]),
            headers={"x-typesafe-request-id": f"accepted-{len(calls)}"},
        )

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(provider)) as provider_client,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(config, db)),
            base_url="http://test",
            headers={"Authorization": f"Bearer {config.dev_api_token}"},
        ) as client,
    ):
        # The factory is replaced only within this test; Worker still resolves the pinned profile.
        monkeypatch.setattr(
            "teaching_assistant.worker.build_adapters",
            lambda worker_settings, resolved: (
                JevEvaluator(worker_settings, resolved, client=provider_client),
                ManualEvidenceExtractor(),
            ),
        )
        batch = await post(
            client,
            "/v1/grading-batches",
            {
                "rubric_version_id": scenario["rubric"]["id"],
                "answer_version_ids": scenario["imported"]["answer_version_ids"][:1],
                "evaluation_profile": profile.id,
            },
        )
        item_id = batch["item_ids"][0]
        worker = Worker(db, config, item_ids=(UUID(item_id),))
        assert await worker.run_once()
        failed = (await client.get(f"/v1/grading-items/{item_id}")).json()
        assert failed["status"] == "retry_wait"
        assert failed["error_code"] == "provider_rate_limited"
        failed_call = failed["execution"]["calls"][0]
        assert failed_call["provider_request_id"] == "limited-attempt"
        assert failed_call["http_status"] == 429 and failed_call["error_retryable"] is True
        # A different Worker instance consumes the persisted ready state, not an in-memory loop.
        restarted = Worker(db, config, item_ids=(UUID(item_id),))
        assert await restarted.run_once()
        completed = (await client.get(f"/v1/grading-items/{item_id}")).json()
        assert completed["status"] == "succeeded"
        assert completed["execution"]["attempts"] == 2
        assert completed["confirmed_score"] is None and completed["review_revision"] == 0
        assert len(completed["assessments"]) == 2 and len(calls) == 3
        assert all(point["evidence"]["citations"] == [] for point in completed["assessments"])
        assert completed["execution"]["calls"][0]["provider_request_id"] == "limited-attempt"
        assert not await restarted.run_once()

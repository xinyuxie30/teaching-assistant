"""Durable two-step grading and trace isolation, using PostgreSQL + a mock provider."""

import json
from uuid import UUID, uuid4

import httpx
import pytest
from conftest import post
from teaching_assistant.api import create_app
from teaching_assistant.cli import bootstrap
from teaching_assistant.deepseek import DeepSeekEvaluator, DeepSeekEvidenceExtractor
from teaching_assistant.profiles import configured_deepseek_profile
from teaching_assistant.worker import Worker
from test_deepseek import response_body
from test_deepseek import settings as deepseek_settings

pytestmark = pytest.mark.postgres


async def test_evidence_retry_reuses_assessment_checkpoint_and_keeps_both_receipts(
    db,
    settings,
    scenario,
    monkeypatch,
):
    config = deepseek_settings(
        dev_api_token=settings.dev_api_token,
        deepseek_evidence_mode="model",
        retry_delay_seconds=0,
    )
    profile = configured_deepseek_profile(config)
    calls, evidence_attempts = [], 0

    def provider(request):
        nonlocal evidence_attempts
        payload = json.loads(request.content)
        system = payload["messages"][0]["content"]
        rules = json.loads(system.split("Teacher-confirmed rules (JSON):\n", 1)[1])
        data = json.loads(payload["messages"][1]["content"])
        evidence = "Independently select" in system
        calls.append((rules["criterion_id"], "extract_evidence" if evidence else "evaluate"))
        if evidence:
            evidence_attempts += 1
            if evidence_attempts == 1:
                return httpx.Response(
                    429, headers={"Retry-After": "0", "x-request-id": "evidence-limited"}
                )
            result = {
                "criterion_id": rules["criterion_id"],
                "quotes": [{"quote": data["student_answer"][:8], "occurrence": 0}],
            }
        else:
            result = {"criterion_id": rules["criterion_id"], "level_id": rules["levels"][-1]["id"]}
        return httpx.Response(
            200,
            json=response_body(json.dumps(result)),
            headers={"x-request-id": f"receipt-{len(calls)}"},
        )

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(provider)) as provider_client,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(config, db)),
            base_url="http://test",
            headers={"Authorization": f"Bearer {config.dev_api_token}"},
        ) as client,
    ):
        monkeypatch.setattr(
            "teaching_assistant.worker.build_adapters",
            lambda worker_settings, resolved: (
                DeepSeekEvaluator(worker_settings, resolved, client=provider_client),
                DeepSeekEvidenceExtractor(worker_settings, resolved, client=provider_client),
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
        assert await Worker(db, config, item_ids=(UUID(item_id),)).run_once()
        trace = (await client.get(f"/v1/grading-items/{item_id}/trace")).json()
        assert trace["status"] == "retry_wait" and trace["budget"]["calls_used"] == 2
        assert len(trace["checkpoints"]) == 1
        first_step = trace["checkpoints"][0]["id"]
        assert trace["calls"][1]["provider_request_id"] == "evidence-limited"
        restarted = Worker(db, config, item_ids=(UUID(item_id),))
        assert await restarted.run_once()
        trace = (await client.get(f"/v1/grading-items/{item_id}/trace")).json()
        item = (await client.get(f"/v1/grading-items/{item_id}")).json()
        assert trace["status"] == "succeeded" and trace["waiting_for"] == "teacher_review"
        assert trace["budget"]["calls_used"] == 5 and len(calls) == 5
        assert len(trace["checkpoints"]) == 4 and len(trace["attempts"]) == 2
        assert first_step in {step["id"] for step in trace["checkpoints"]}
        assert sum(kind == "evaluate" for _, kind in calls) == 2
        assert all(step["status"] == "checkpointed" for step in trace["plan"])
        successful_evidence = [
            call
            for call in trace["calls"]
            if call["kind"] == "extract_evidence" and call["status"] == "succeeded"
        ]
        assert len(successful_evidence) == 2
        assert all(call["usage"]["input_tokens"] == 10 for call in successful_evidence)
        successful = [call for call in trace["calls"] if call["status"] == "succeeded"]
        assert all(call["provider_fingerprint"] == "fixture-revision" for call in successful)
        limited = next(call for call in trace["calls"] if call["http_status"] == 429)
        assert limited["provider_fingerprint"] is None
        assert trace["profile"]["fingerprint"] != "fixture-revision"
        assert {
            call["id"]: call["provider_fingerprint"] for call in item["execution"]["calls"]
        } == {call["id"]: call["provider_fingerprint"] for call in trace["calls"]}
        assert item["confirmed_score"] is None and item["review_revision"] == 0
        for point in item["assessments"]:
            assert point["evidence_support"] == "requires_teacher_review"
            citation = point["evidence"]["citations"][0]
            assert item["answer"]["text"][citation["start"] : citation["end"]] == citation["quote"]
            assert point["evidence"]["answer_version_id"] == item["answer"]["id"]
        assert not await restarted.run_once()


@pytest.mark.parametrize("failed_kind", ["evaluate", "extract_evidence"])
async def test_failed_response_fingerprint_is_saved_without_a_checkpoint(
    db, settings, scenario, monkeypatch, failed_kind
):
    config = deepseek_settings(dev_api_token=settings.dev_api_token, deepseek_evidence_mode="model")
    profile = configured_deepseek_profile(config)

    def provider(request):
        system = json.loads(request.content)["messages"][0]["content"]
        rules = json.loads(system.split("Teacher-confirmed rules (JSON):\n", 1)[1])
        kind = "extract_evidence" if "Independently select" in system else "evaluate"
        result = {"criterion_id": rules["criterion_id"]}
        if kind != failed_kind:
            result["level_id"] = rules["levels"][-1]["id"]
        # Missing required level_id/quotes makes this response invalid after metadata is parsed.
        return httpx.Response(
            200, json=response_body(json.dumps(result), system_fingerprint="failed-revision")
        )

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(provider)) as provider_client,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(config, db)),
            base_url="http://test",
            headers={"Authorization": f"Bearer {config.dev_api_token}"},
        ) as client,
    ):
        monkeypatch.setattr(
            "teaching_assistant.worker.build_adapters",
            lambda worker_settings, resolved: (
                DeepSeekEvaluator(worker_settings, resolved, client=provider_client),
                DeepSeekEvidenceExtractor(worker_settings, resolved, client=provider_client),
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
        assert await Worker(db, config, item_ids=(UUID(item_id),)).run_once()
        trace = (await client.get(f"/v1/grading-items/{item_id}/trace")).json()
        failed = next(call for call in trace["calls"] if call["status"] == "failed")
        assert failed["kind"] == failed_kind
        assert failed["provider_fingerprint"] == "failed-revision"
        assert failed["http_status"] == 200 and failed["usage"]["input_tokens"] == 10
        assert all(step["call_id"] != failed["id"] for step in trace["checkpoints"])


async def test_trace_uses_object_ownership_not_knowledge_of_item_id(db, settings, client, scenario):
    item_id = scenario["batch"]["item_ids"][0]
    own = await client.get(f"/v1/grading-items/{item_id}/trace")
    assert own.status_code == 200 and own.json()["waiting_for"] == "matching_worker"
    other_settings = settings.model_copy(update={"dev_api_token": f"other-{uuid4().hex}"})
    await bootstrap(db, other_settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(other_settings, db)),
        base_url="http://test",
        headers={"Authorization": f"Bearer {other_settings.dev_api_token}"},
    ) as other:
        response = await other.get(f"/v1/grading-items/{item_id}/trace")
        assert response.status_code == 404 and response.json()["code"] == "not_found"


async def test_preflight_failure_does_not_reserve_any_call(db, settings, scenario, monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("Preflight must not reach a model transport")

    monkeypatch.setattr("teaching_assistant.deepseek.post_json", forbidden)
    config = deepseek_settings(
        dev_api_token=settings.dev_api_token, deepseek_max_input_characters=1000
    )
    profile = configured_deepseek_profile(config)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(config, db)),
        base_url="http://test",
        headers={"Authorization": f"Bearer {config.dev_api_token}"},
    ) as client:
        imported = await post(
            client,
            f"/v1/assignments/{scenario['assignment']['id']}/imports",
            {
                "rows": [
                    {
                        "student_id": scenario["workspace"]["students"][0]["id"],
                        "question_version_id": scenario["question"]["id"],
                        "external_submission_id": "oversized-for-profile",
                        "source_revision": "v1",
                        "answer_text": "长" * 2000,
                    }
                ],
            },
        )
        batch = await post(
            client,
            "/v1/grading-batches",
            {
                "rubric_version_id": scenario["rubric"]["id"],
                "answer_version_ids": imported["answer_version_ids"],
                "evaluation_profile": profile.id,
            },
        )
        item_id = batch["item_ids"][0]
        assert await Worker(db, config, item_ids=(UUID(item_id),)).run_once()
        trace = (await client.get(f"/v1/grading-items/{item_id}/trace")).json()
        assert trace["status"] == "failed" and trace["error_code"] == "model_input_budget_exceeded"
        assert trace["calls"] == [] and trace["budget"]["calls_used"] == 0

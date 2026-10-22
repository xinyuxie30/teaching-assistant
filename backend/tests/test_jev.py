import json
from uuid import uuid4

import httpx
import pytest
from teaching_assistant.domain import AssessmentInput, Criterion, Level, ModelError
from teaching_assistant.jev import JevEvaluator
from teaching_assistant.profiles import configured_jev_profile
from teaching_assistant.settings import Settings


def settings(**overrides):
    values = {
        "jev_model": "jev-1.14.0",
        "jev_api_key": "test-key",
        "jev_endpoint": "https://api.typesafe.ai/v1/systemone",
        "jev_proxy": None,
        "call_timeout_seconds": 5,
        "lease_seconds": 10,
    }
    values.update(overrides)
    return Settings(**values)


def request_for(profile, answer="统一规则并追溯"):
    criterion = Criterion(
        id="fixed_rule",
        description="是否说明固定规则",
        maximum_points="6.00",
        levels=[
            Level(id="missing", ordinal=0, description="未说明", points="0.00"),
            Level(id="partial", ordinal=1, description="部分说明", points="3.00"),
            Level(id="complete", ordinal=2, description="明确说明", points="6.00"),
        ],
    )
    return AssessmentInput(
        question_version_id=uuid4(),
        answer_version_id=uuid4(),
        rubric_version_id=uuid4(),
        question="说明两个作用。",
        reference="统一规则；可追溯。",
        answer=answer,
        global_instructions="只评估当前评分点。",
        criterion=criterion,
        profile=profile.id,
    )


def response_body(payload, *, model="jev-1.14.0", legend=None, probabilities=None, score=None):
    levels = payload["questions"]["criterion"]["criteria"]
    return {
        "model": model,
        "answers": {
            "criterion": {
                "type": "score",
                "score": float(len(levels) - 1) if score is None else score,
                "confidence": 1.0,
                "legend": legend
                if legend is not None
                else {str(i): text for i, text in enumerate(levels)},
                "probabilities": probabilities
                if probabilities is not None
                else {str(i): float(i == len(levels) - 1) for i in range(len(levels))},
            }
        },
        "usage": {"input_tokens": 10, "output_tokens": 3},
    }


async def test_assess_captures_provider_request_id_and_usage():
    config = settings()
    profile = configured_jev_profile(config)
    request = request_for(profile)
    captured = {}

    def handler(http_request):
        payload = json.loads(http_request.content)
        captured.update(payload)
        return httpx.Response(
            200,
            json=response_body(payload),
            headers={"x-typesafe-request-id": "req_test_123"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        output = await JevEvaluator(config, profile, client=client).assess(request)

    assert output.provider_request_id == "req_test_123"
    assert output.returned_model == "jev-1.14.0"
    assert output.candidate_level_id == "complete"
    assert output.provider_score == 2.0
    assert output.usage == {"input_tokens": 10, "output_tokens": 3}
    assert output.prompt_hash


async def test_payload_contains_no_student_identity():
    config = settings()
    profile = configured_jev_profile(config)
    captured = {}

    def handler(http_request):
        captured.update(json.loads(http_request.content))
        return httpx.Response(200, json=response_body(captured))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await JevEvaluator(config, profile, client=client).assess(request_for(profile))

    text = json.dumps(captured, ensure_ascii=False)
    assert "student_id" not in text
    assert "student_name" not in text
    assert set(captured) == {"model", "state", "questions"}


async def test_wrong_legend_is_rejected_as_invalid_score():
    config = settings()
    profile = configured_jev_profile(config)

    def handler(http_request):
        payload = json.loads(http_request.content)
        body = response_body(payload, legend={"0": "x", "1": "y", "2": "z"})
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ModelError) as raised:
            await JevEvaluator(config, profile, client=client).assess(request_for(profile))
    assert raised.value.code == "provider_invalid_score"
    assert raised.value.retryable is True


async def test_model_version_mismatch_is_not_retryable():
    config = settings()
    profile = configured_jev_profile(config)

    def handler(http_request):
        payload = json.loads(http_request.content)
        return httpx.Response(200, json=response_body(payload, model="jev-9.9.9"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ModelError) as raised:
            await JevEvaluator(config, profile, client=client).assess(request_for(profile))
    assert raised.value.code == "provider_model_version_mismatch"
    assert raised.value.retryable is False


async def test_error_response_also_captures_request_id():
    config = settings()
    profile = configured_jev_profile(config)

    def handler(http_request):
        return httpx.Response(
            429,
            json={"detail": "rate limited"},
            headers={"x-typesafe-request-id": "req_rate_limited", "Retry-After": "1"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ModelError) as raised:
            await JevEvaluator(config, profile, client=client).assess(request_for(profile))
    assert raised.value.code == "provider_rate_limited"
    assert raised.value.retryable is True
    assert raised.value.provider_request_id == "req_rate_limited"
    assert raised.value.http_status == 429
    assert raised.value.retry_after_seconds == 1


async def test_input_budget_is_checked_before_any_request():
    config = settings(jev_max_input_characters=1000)
    profile = configured_jev_profile(config)
    calls = []

    def handler(http_request):
        calls.append(http_request)
        payload = json.loads(http_request.content)
        return httpx.Response(200, json=response_body(payload))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ModelError) as raised:
            await JevEvaluator(config, profile, client=client).assess(
                request_for(profile, answer="字" * 2000)
            )
    assert raised.value.code == "model_input_budget_exceeded"
    assert raised.value.retryable is False
    assert calls == []


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (401, "provider_auth_error", False),
        (403, "provider_auth_error", False),
        (400, "provider_request_rejected", False),
        (408, "provider_temporarily_unavailable", True),
        (500, "provider_temporarily_unavailable", True),
        (503, "provider_temporarily_unavailable", True),
    ],
)
async def test_http_failure_classification(status, code, retryable):
    config = settings()
    profile = configured_jev_profile(config)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(status))
    ) as client:
        with pytest.raises(ModelError) as raised:
            await JevEvaluator(config, profile, client=client).assess(request_for(profile))
    assert raised.value.code == code and raised.value.retryable is retryable
    assert raised.value.http_status == status


async def test_redirect_is_rejected_without_sending_credentials_to_target():
    calls = []
    config = settings()
    profile = configured_jev_profile(config)

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(307, headers={"Location": "https://unrelated.invalid/collect"})

    # Even a client configured to follow redirects must be overridden per request.
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        with pytest.raises(ModelError) as raised:
            await JevEvaluator(config, profile, client=client).assess(request_for(profile))
    assert raised.value.code == "provider_request_rejected"
    assert raised.value.retryable is False
    assert calls == [config.jev_endpoint]


async def test_tied_distribution_abstains_instead_of_rounding_continuous_score():
    config = settings()
    profile = configured_jev_profile(config)

    def handler(request):
        return httpx.Response(
            200,
            json=response_body(
                json.loads(request.content),
                probabilities={"0": 0.5, "1": 0.0, "2": 0.5},
                score=1.0,
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        output = await JevEvaluator(config, profile, client=client).assess(request_for(profile))
    assert output.candidate_level_id is None
    assert output.provider_score == 1.0
    assert output.probabilities == {"missing": 0.5, "partial": 0.0, "complete": 0.5}


async def test_invalid_probability_mass_is_rejected():
    config = settings()
    profile = configured_jev_profile(config)

    def handler(request):
        return httpx.Response(
            200,
            json=response_body(
                json.loads(request.content),
                probabilities={"0": 0.0, "1": 0.1, "2": 1.0},
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ModelError) as raised:
            await JevEvaluator(config, profile, client=client).assess(request_for(profile))
    assert raised.value.code == "invalid_distribution"
    assert raised.value.retryable is True

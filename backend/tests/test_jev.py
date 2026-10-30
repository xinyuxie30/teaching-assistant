import json
from uuid import uuid4

import httpx
import pytest
from teaching_assistant.domain import AssessmentInput, Criterion, Level, ModelError, digest
from teaching_assistant.jev import JevEvaluator
from teaching_assistant.profiles import Profile, configured_jev_profile
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


def profile_for_adapter(config, adapter_version):
    manifest = {**configured_jev_profile(config).manifest, "adapter_version": adapter_version}
    return Profile("jev-" + digest(manifest), manifest)


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
        (402, "provider_insufficient_balance", False),
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
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status,
            text="private provider diagnostic",
            headers={"x-typesafe-request-id": "failure-receipt", "Retry-After": "7"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ModelError) as raised:
            await JevEvaluator(config, profile, client=client).assess(request_for(profile))
    assert raised.value.code == code and raised.value.retryable is retryable
    assert raised.value.http_status == status
    assert raised.value.provider_request_id == "failure-receipt"
    assert len(calls) == 1 and "private" not in str(raised.value)


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


@pytest.mark.parametrize(
    ("adapter_version", "reported_score", "accepted"),
    [
        ("typesafe-http-score-v1", 7.53, False),
        ("typesafe-http-score-v1", 7.56, True),
        ("typesafe-http-score-v2", 7.53, True),
        ("typesafe-http-score-v2", 7.8, False),
    ],
)
async def test_ten_level_wire_rounding_has_a_bounded_mean_tolerance(
    adapter_version, reported_score, accepted
):
    """Replay the real 2026-10-30 distribution; a genuine mean contradiction still fails."""
    config = settings()
    profile = profile_for_adapter(config, adapter_version)
    request = request_for(profile).model_copy(
        update={
            "criterion": Criterion(
                id="band_0",
                description="An independently scored benchmark total band",
                maximum_points="9",
                levels=[
                    Level(id=f"p{i}", ordinal=i, description=f"Level {i}", points=str(i))
                    for i in range(10)
                ],
            )
        }
    )
    rounded = [0.0, 0.0, 0.0, 0.01, 0.01, 0.05, 0.12, 0.22, 0.33, 0.26]

    def handler(http_request):
        return httpx.Response(
            200,
            json=response_body(
                json.loads(http_request.content),
                probabilities={str(i): weight for i, weight in enumerate(rounded)},
                score=reported_score,
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = JevEvaluator(config, profile, client=client)
        if not accepted:
            with pytest.raises(ModelError) as raised:
                await adapter.assess(request)
            assert raised.value.code == "provider_invalid_score"
            return
        output = await adapter.assess(request)
    assert output.provider_score == reported_score
    assert output.adapter_version == adapter_version
    assert output.candidate_level_id == "p8"
    assert list(output.probabilities.values()) == rounded


async def test_legacy_adapter_keeps_strict_probability_mass_without_normalizing():
    config = settings()
    profile = profile_for_adapter(config, "typesafe-http-score-v1")

    def handler(http_request):
        return httpx.Response(
            200,
            json=response_body(
                json.loads(http_request.content),
                probabilities={"0": 0.33, "1": 0.33, "2": 0.33},
                score=1.0,
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ModelError) as raised:
            await JevEvaluator(config, profile, client=client).assess(request_for(profile))
    assert raised.value.code == "invalid_distribution"


@pytest.mark.parametrize("adapter_version", [None, "typesafe-http-score-v999"])
def test_unknown_adapter_version_is_rejected_before_dispatch(adapter_version):
    config = settings()
    profile = profile_for_adapter(config, adapter_version)
    with pytest.raises(ModelError) as raised:
        JevEvaluator(config, profile)
    assert raised.value.code == "unsupported_adapter_version"
    assert raised.value.retryable is False


@pytest.mark.parametrize(
    ("probabilities", "score", "selected"),
    [
        ({"0": 0.33, "1": 0.33, "2": 0.33}, 1.0, None),
        ({"0": 0.0, "1": 0.34, "2": 0.65}, 1.66, "complete"),
        ({"0": 1.0, "1": 0.0, "2": 0.0}, 0.0, "missing"),
    ],
)
async def test_rounded_probability_mass_preserves_zeros_ties_and_argmax(
    probabilities, score, selected
):
    config = settings()
    profile = configured_jev_profile(config)

    def handler(http_request):
        return httpx.Response(
            200,
            json=response_body(
                json.loads(http_request.content), probabilities=probabilities, score=score
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        output = await JevEvaluator(config, profile, client=client).assess(request_for(profile))
    assert output.candidate_level_id == selected
    assert sum(output.probabilities.values()) == pytest.approx(1)
    for index, level in enumerate(("missing", "partial", "complete")):
        if probabilities[str(index)] == 0:
            assert output.probabilities[level] == 0


@pytest.mark.parametrize(
    "probabilities",
    [
        {"0": 0.32, "1": 0.32, "2": 0.32},
        {"0": 0.35, "1": 0.35, "2": 0.35},
        {"0": 0.0, "1": 0.0, "2": 0.0},
        {"0": -0.001, "1": 0.501, "2": 0.5},
        {"0": float("nan"), "1": 0.5, "2": 0.5},
    ],
)
async def test_rounded_probability_policy_rejects_out_of_bound_mass(probabilities):
    config = settings()
    profile = configured_jev_profile(config)

    def handler(http_request):
        # Explicit JSON text permits the invalid NaN response used by this negative test.
        body = response_body(json.loads(http_request.content), probabilities=probabilities, score=1)
        return httpx.Response(
            200, content=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ModelError) as raised:
            await JevEvaluator(config, profile, client=client).assess(request_for(profile))
    assert raised.value.code == "invalid_distribution"

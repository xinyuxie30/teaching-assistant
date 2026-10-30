"""TypeSafe System One / Score adapter. One HTTP request, zero hidden retries."""

import json
from decimal import Decimal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from teaching_assistant.domain import (
    AssessmentInput,
    DomainError,
    Evaluation,
    ModelError,
    candidate,
    digest,
)
from teaching_assistant.model_http import retry_after
from teaching_assistant.profiles import JEV_ADAPTER_VERSION, Profile
from teaching_assistant.settings import Settings


class _Score(BaseModel):
    model_config = ConfigDict(extra="ignore")
    type: str
    score: float = Field(ge=0, allow_inf_nan=False)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    probabilities: dict[str, float]
    legend: dict[str, str]


class _Usage(BaseModel):
    model_config = ConfigDict(extra="ignore")
    input_tokens: int = Field(ge=0, strict=True)
    output_tokens: int = Field(ge=0, strict=True)


class _Response(BaseModel):
    model_config = ConfigDict(extra="ignore")
    model: str
    answers: dict[str, _Score]


def _validated_probabilities(score: _Score, size: int) -> dict[str, float]:
    """Validate finite precision wire values before adapting the domain distribution.

    TypeSafe's OpenAPI specifies an expected score and probabilities summing to
    approximately one. A captured jev-1.14.0 response (2026-10-30) reports both to
    two decimal places: score 7.53, while the rounded probabilities imply 7.56.
    Allow only the feasible means of a normalized distribution within +/-0.005
    of each reported probability, plus +/-0.005 for the reported score. This
    bounded compatibility policy does not change argmax or turn Score into marks.
    Source: https://api.typesafe.ai/openapi.json (ScoreAnswer).
    """
    half_quantum = Decimal("0.005")
    weights = [Decimal(str(score.probabilities[str(i)])) for i in range(size)]
    if any(not weight.is_finite() or not 0 <= weight <= 1 for weight in weights):
        raise DomainError("invalid_distribution", "Probabilities must be finite and in [0, 1]")
    lower = [max(Decimal(0), weight - half_quantum) for weight in weights]
    upper = [min(Decimal(1), weight + half_quantum) for weight in weights]
    if not sum(lower) <= 1 <= sum(upper):
        raise DomainError("invalid_distribution", "Probability mass exceeds rounding bounds")

    def extreme_mean(indices):
        remaining = Decimal(1) - sum(lower)
        mean = sum(index * weight for index, weight in enumerate(lower))
        for index in indices:
            added = min(remaining, upper[index] - lower[index])
            mean += index * added
            remaining -= added
        return mean

    minimum = extreme_mean(range(size))
    maximum = extreme_mean(reversed(range(size)))
    reported = Decimal(str(score.score))
    if reported + half_quantum < minimum or reported - half_quantum > maximum:
        raise ValueError("Score contradicts the distribution beyond rounding bounds")
    # Domain probabilities sum to one; a common factor preserves zeros, ties and argmax.
    total = sum(weights)
    return {str(index): float(weight / total) for index, weight in enumerate(weights)}


class JevEvaluator:
    adapter_version = JEV_ADAPTER_VERSION

    def __init__(
        self, settings: Settings, profile: Profile, *, client: httpx.AsyncClient | None = None
    ):
        if profile.manifest["provider"] != "typesafe":
            raise ValueError("JevEvaluator requires a TypeSafe profile")
        adapter_version = profile.manifest.get("adapter_version")
        if adapter_version not in {"typesafe-http-score-v1", "typesafe-http-score-v2"}:
            raise ModelError("unsupported_adapter_version", retryable=False)
        if not settings.jev_api_key or not settings.jev_api_key.get_secret_value().strip():
            raise ModelError("model_not_configured", retryable=False)
        self.settings, self.profile, self.client = settings, profile, client
        self.requested_model = profile.manifest["model"]
        self.adapter_version = adapter_version

    def prepare(self, request: AssessmentInput) -> dict:
        if request.profile != self.profile.id:
            raise ModelError("evaluation_profile_mismatch", retryable=False)
        levels = sorted(request.criterion.levels, key=lambda level: level.ordinal)
        payload = {
            "model": self.requested_model,
            "state": {
                "question": request.question,
                "reference": request.reference,
                "student_answer": request.answer,
            },
            "questions": {
                "criterion": {
                    "type": "score",
                    "instructions": {
                        "task": self.profile.manifest["instructions"],
                        "teacher_rules": request.global_instructions,
                        "criterion": request.criterion.description,
                    },
                    "criteria": [level.description for level in levels],
                }
            },
        }
        characters = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        if characters > self.profile.manifest["max_input_characters"]:
            raise ModelError("model_input_budget_exceeded", retryable=False)
        return payload

    def prompt_hash(self, request: AssessmentInput) -> str:
        return digest(self.prepare(request)["questions"])

    async def _post(self, client: httpx.AsyncClient, payload: dict) -> tuple[dict, str | None, int]:
        request_id, status = None, None
        try:
            async with client.stream(
                "POST",
                self.profile.manifest["endpoint"],
                json=payload,
                headers={
                    "Authorization": f"Bearer {self.settings.jev_api_key.get_secret_value()}",
                    "Content-Type": "application/json",
                },
                timeout=self.profile.manifest["call_timeout_seconds"],
                follow_redirects=False,
            ) as response:
                request_id = (
                    response.headers.get("x-typesafe-request-id")
                    or response.headers.get("x-request-id")
                    or response.headers.get("request-id")
                )
                request_id = request_id[:255] if request_id else None
                status = response.status_code
                if not 200 <= status < 300:
                    if status in {401, 403}:
                        code = "provider_auth_error"
                    elif status == 402:
                        code = "provider_insufficient_balance"
                    elif status == 429:
                        code = "provider_rate_limited"
                    elif status in {408, 425} or status >= 500:
                        code = "provider_temporarily_unavailable"
                    else:
                        code = "provider_request_rejected"
                    raise ModelError(
                        code,
                        retryable=status in {408, 425, 429} or status >= 500,
                        retry_after_seconds=retry_after(response.headers.get("retry-after")),
                        provider_request_id=request_id,
                        http_status=status,
                    )
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > 2_000_000:
                        raise ModelError(
                            "provider_response_too_large",
                            retryable=False,
                            provider_request_id=request_id,
                            http_status=status,
                        )
                    chunks.append(chunk)
                try:
                    body = json.loads(b"".join(chunks))
                    if not isinstance(body, dict):
                        raise ValueError("Expected a response object")
                except (ValueError, UnicodeDecodeError) as exc:
                    raise ModelError(
                        "provider_invalid_json",
                        retryable=True,
                        provider_request_id=request_id,
                        http_status=status,
                    ) from exc
                return body, request_id, status
        except httpx.TimeoutException as exc:
            raise ModelError(
                "provider_timeout",
                retryable=True,
                provider_request_id=request_id,
                http_status=status,
            ) from exc
        except httpx.TransportError as exc:
            raise ModelError(
                "provider_transport_error",
                retryable=True,
                provider_request_id=request_id,
                http_status=status,
            ) from exc

    async def assess(self, request: AssessmentInput) -> Evaluation:
        payload = self.prepare(request)
        if self.client is not None:
            raw, request_id, status = await self._post(self.client, payload)
        else:
            # Explicit proxy wins over the process environment; default transport has zero retries.
            async with httpx.AsyncClient(
                proxy=self.settings.jev_proxy, follow_redirects=False
            ) as client:
                raw, request_id, status = await self._post(client, payload)
        usage = None
        returned_model = raw["model"][:120] if isinstance(raw.get("model"), str) else None
        try:
            if raw.get("usage") is not None:
                usage = _Usage.model_validate(raw["usage"]).model_dump()
            parsed = _Response.model_validate(raw)
            if parsed.model != self.requested_model:
                raise ModelError("provider_model_version_mismatch", retryable=False)
            if set(parsed.answers) != {"criterion"}:
                raise ValueError("Missing or extra assessment")
            score = parsed.answers["criterion"]
            levels = sorted(request.criterion.levels, key=lambda level: level.ordinal)
            expected_legend = {str(index): level.description for index, level in enumerate(levels)}
            if (
                score.type != "score"
                or score.legend != expected_legend
                or set(score.probabilities) != set(expected_legend)
                or score.score > len(levels) - 1
            ):
                raise ValueError("Score shape or level mapping changed")
            # Frozen historical profiles keep their original wire validation semantics.
            normalized = (
                score.probabilities
                if self.adapter_version == "typesafe-http-score-v1"
                else _validated_probabilities(score, len(levels))
            )
            probabilities = {level.id: normalized[str(index)] for index, level in enumerate(levels)}
            output = Evaluation(
                probabilities=probabilities,
                provider_score=score.score,
                provider_confidence=score.confidence,
                requested_model=self.requested_model,
                returned_model=parsed.model,
                adapter_version=self.adapter_version,
                usage=usage,
                provider_request_id=request_id,
                prompt_hash=digest(payload["questions"]),
                http_status=status,
            )
            selected = candidate(output, request.criterion)
            if self.adapter_version == "typesafe-http-score-v1":
                expected_score = sum(
                    index * score.probabilities[str(index)] for index in range(len(levels))
                )
                if abs(expected_score - score.score) > 0.02:
                    raise ValueError("Score contradicts the distribution")
            return output.model_copy(update={"candidate_level_id": selected})
        except ModelError as exc:
            exc.usage, exc.returned_model = usage, returned_model
            exc.provider_request_id, exc.http_status = request_id, status
            raise
        except (DomainError, ValidationError, ValueError, TypeError, KeyError) as exc:
            raise ModelError(
                exc.code if isinstance(exc, DomainError) else "provider_invalid_score",
                retryable=True,
                usage=usage,
                returned_model=returned_model,
                provider_request_id=request_id,
                http_status=status,
            ) from exc

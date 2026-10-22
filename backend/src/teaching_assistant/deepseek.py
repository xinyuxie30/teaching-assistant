"""DeepSeek JSON assessment and independent evidence, never autonomous business writes."""

import json

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from teaching_assistant.domain import (
    AssessmentInput,
    Contract,
    DomainError,
    Evaluation,
    Evidence,
    EvidenceOutput,
    ModelError,
    ModelMetadata,
    candidate,
    digest,
    validate_evidence,
)
from teaching_assistant.grading_context import DEEPSEEK_ADAPTER_VERSION, EVIDENCE_VERSION, messages
from teaching_assistant.model_http import post_json, strict_json
from teaching_assistant.profiles import Profile
from teaching_assistant.settings import Settings


class _Decision(Contract):
    criterion_id: str
    level_id: str | None


class _Quote(Contract):
    quote: str = Field(min_length=1, max_length=100000)
    occurrence: int = Field(ge=0, le=100000, strict=True)


class _Quotes(Contract):
    criterion_id: str
    quotes: list[_Quote] = Field(max_length=20)


class _Usage(BaseModel):
    model_config = ConfigDict(extra="ignore")
    prompt_tokens: int = Field(ge=0, strict=True)
    completion_tokens: int = Field(ge=0, strict=True)
    total_tokens: int | None = Field(default=None, ge=0, strict=True)
    prompt_cache_hit_tokens: int | None = Field(default=None, ge=0, strict=True)
    prompt_cache_miss_tokens: int | None = Field(default=None, ge=0, strict=True)

    def normalized(self):
        if (
            self.total_tokens is not None
            and self.total_tokens != self.prompt_tokens + self.completion_tokens
        ):
            raise ValueError("Usage totals disagree")
        result = {"input_tokens": self.prompt_tokens, "output_tokens": self.completion_tokens}
        hit, miss = self.prompt_cache_hit_tokens, self.prompt_cache_miss_tokens
        if hit is not None or miss is not None:
            if hit is None or miss is None or hit + miss != self.prompt_tokens:
                raise ValueError("Cache usage is incomplete or inconsistent")
            result.update(input_cached_tokens=hit, input_uncached_tokens=miss)
        return result


def locate_quotes(answer: str, quotes: list[_Quote]) -> list[Evidence]:
    result, seen = [], set()
    for value in quotes:
        start = -1
        for _ in range(value.occurrence + 1):
            start = answer.find(value.quote, start + 1)
            if start < 0:
                raise DomainError("invalid_evidence", "Quoted occurrence is not in this answer")
        end = start + len(value.quote)
        if (start, end) in seen:
            raise DomainError("invalid_evidence", "Duplicate citation")
        seen.add((start, end))
        result.append(Evidence(quote=value.quote, start=start, end=end))
    return result


class DeepSeekAdapter:
    adapter_version = DEEPSEEK_ADAPTER_VERSION
    kind = "evaluate"

    def __init__(
        self, settings: Settings, profile: Profile, *, client: httpx.AsyncClient | None = None
    ):
        if profile.manifest["provider"] != "deepseek":
            raise ValueError("DeepSeekAdapter requires a DeepSeek profile")
        if (
            not settings.deepseek_api_key
            or not settings.deepseek_api_key.get_secret_value().strip()
        ):
            raise ModelError("model_not_configured", retryable=False)
        self.settings, self.profile, self.client = settings, profile, client
        self.requested_model = profile.manifest["model"]

    def prepare(self, request: AssessmentInput) -> dict:
        if request.profile != self.profile.id:
            raise ModelError("evaluation_profile_mismatch", retryable=False)
        if (
            self.kind == "extract_evidence"
            and self.profile.manifest["evidence_strategy"] != EVIDENCE_VERSION
        ):
            raise ModelError("evidence_strategy_mismatch", retryable=False)
        payload = {
            "model": self.requested_model,
            "messages": messages(request, self.kind),
            "response_format": {"type": "json_object"},
            "thinking": {"type": self.profile.manifest["thinking"]},
            "temperature": self.profile.manifest["temperature"],
            "max_tokens": self.profile.manifest["max_output_tokens"],
            "stream": False,
        }
        if (
            len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
            > self.profile.manifest["max_input_characters"]
        ):
            raise ModelError("model_input_budget_exceeded", retryable=False)
        return payload

    def prompt_hash(self, request: AssessmentInput) -> str:
        return digest(self.prepare(request))

    async def complete(self, request: AssessmentInput) -> tuple[dict, ModelMetadata]:
        payload = self.prepare(request)
        kwargs = {
            "endpoint": self.profile.manifest["endpoint"],
            "payload": payload,
            "api_key": self.settings.deepseek_api_key.get_secret_value(),
            "timeout_seconds": self.profile.manifest["call_timeout_seconds"],
        }
        if self.client is not None:
            raw, request_id, status = await post_json(self.client, **kwargs)
        else:
            async with httpx.AsyncClient(
                proxy=self.settings.deepseek_proxy, trust_env=False, follow_redirects=False
            ) as client:
                raw, request_id, status = await post_json(client, **kwargs)
        usage = None
        returned = raw.get("model")
        returned = returned[:120] if isinstance(returned, str) else None
        body_id = raw.get("id")
        request_id = request_id or (body_id[:255] if isinstance(body_id, str) else None)
        fingerprint = raw.get("system_fingerprint")
        fingerprint = fingerprint[:255] if isinstance(fingerprint, str) else None
        try:
            if raw.get("usage") is not None:
                usage = _Usage.model_validate(raw["usage"]).normalized()
            if raw.get("model") != self.profile.manifest["expected_returned_model"]:
                raise ModelError("provider_model_version_mismatch", retryable=False)
            choices = raw.get("choices")
            if (
                not isinstance(choices, list)
                or len(choices) != 1
                or not isinstance(choices[0], dict)
            ):
                raise ValueError("Expected exactly one choice")
            choice = choices[0]
            if choice.get("finish_reason") != "stop":
                raise ModelError(
                    "provider_output_truncated"
                    if choice.get("finish_reason") == "length"
                    else "provider_incomplete_response",
                    retryable=False,
                )
            message = choice.get("message")
            if not isinstance(message, dict) or message.get("role") != "assistant":
                raise ValueError("Expected assistant response")
            if message.get("tool_calls") or message.get("function_call") or message.get("refusal"):
                raise ModelError("provider_unexpected_action_or_refusal", retryable=False)
            content = message.get("content")
            if not isinstance(content, str) or not content.strip():
                raise ModelError("provider_empty_content", retryable=True)
            parsed = strict_json(content)
            if not isinstance(parsed, dict):
                raise ValueError("Expected JSON object")
            return parsed, ModelMetadata(
                requested_model=self.requested_model,
                returned_model=returned,
                adapter_version=self.adapter_version,
                usage=usage,
                provider_request_id=request_id,
                http_status=status,
                prompt_hash=digest(payload),
                provider_fingerprint=fingerprint,
            )
        except (ModelError, ValidationError, ValueError, TypeError, RecursionError) as exc:
            failure = (
                exc
                if isinstance(exc, ModelError)
                else ModelError("provider_invalid_response", retryable=True)
            )
            failure.usage, failure.returned_model = usage, returned
            failure.provider_fingerprint = fingerprint
            failure.provider_request_id, failure.http_status = request_id, status
            raise failure from None

    @staticmethod
    def invalid_output(exc: Exception, metadata: ModelMetadata) -> ModelError:
        return ModelError(
            exc.code if isinstance(exc, DomainError) else "provider_invalid_output",
            retryable=True,
            usage=metadata.usage,
            returned_model=metadata.returned_model,
            provider_fingerprint=metadata.provider_fingerprint,
            provider_request_id=metadata.provider_request_id,
            http_status=metadata.http_status,
        )


class DeepSeekEvaluator(DeepSeekAdapter):
    async def assess(self, request: AssessmentInput) -> Evaluation:
        raw, metadata = await self.complete(request)
        try:
            parsed = _Decision.model_validate(raw)
            if parsed.criterion_id != request.criterion.id:
                raise ValueError("Wrong criterion")
            result = Evaluation(candidate_level_id=parsed.level_id, **metadata.model_dump())
            candidate(result, request.criterion)
            return result
        except (DomainError, ValidationError, ValueError) as exc:
            raise self.invalid_output(exc, metadata) from None


class DeepSeekEvidenceExtractor(DeepSeekAdapter):
    kind = "extract_evidence"
    adapter_version = EVIDENCE_VERSION

    async def extract(self, request: AssessmentInput) -> EvidenceOutput:
        raw, metadata = await self.complete(request)
        try:
            parsed = _Quotes.model_validate(raw)
            if parsed.criterion_id != request.criterion.id:
                raise ValueError("Wrong criterion")
            result = EvidenceOutput(
                citations=locate_quotes(request.answer, parsed.quotes),
                absence_claim=False,
                extractor_version=EVIDENCE_VERSION,
                model_metadata=metadata,
            )
            validate_evidence(request.answer, result)
            return result
        except (DomainError, ValidationError, ValueError) as exc:
            raise self.invalid_output(exc, metadata) from None

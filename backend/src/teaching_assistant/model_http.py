"""Bounded JSON transport; retries and durable receipts belong to the runtime."""

import json
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from teaching_assistant.domain import ModelError


def retry_after(value: str | None) -> int | None:
    if not value:
        return None
    try:
        seconds = int(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            seconds = int((when - datetime.now(UTC)).total_seconds()) + 1
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0, min(seconds, 3600))


def strict_json(value: str | bytes):
    def pairs(entries):
        result = {}
        for key, item in entries:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = item
        return result

    def constant(_value):
        raise ValueError("Non-finite JSON number")

    return json.loads(value, object_pairs_hook=pairs, parse_constant=constant)


async def post_json(client, *, endpoint, payload, api_key, timeout_seconds):
    request_id, status = None, None
    try:
        async with client.stream(
            "POST",
            endpoint,
            json=payload,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=timeout_seconds,
            follow_redirects=False,
        ) as response:
            request_id = response.headers.get("x-request-id") or response.headers.get("request-id")
            request_id = request_id[:255] if request_id else None
            status = response.status_code
            if not 200 <= status < 300:
                code = (
                    "provider_auth_error"
                    if status in {401, 403}
                    else "provider_insufficient_balance"
                    if status == 402
                    else "provider_rate_limited"
                    if status == 429
                    else "provider_temporarily_unavailable"
                    if status in {408, 425} or status >= 500
                    else "provider_request_rejected"
                )
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
                body = strict_json(b"".join(chunks))
                if not isinstance(body, dict):
                    raise ValueError("Expected an object")
            except (ValueError, UnicodeDecodeError, RecursionError) as exc:
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

"""Prepare and checkpoint model context inside the fenced dispatch transaction."""

import json

from sqlalchemy import select

from teaching_assistant.db import AgentContextCheckpoint
from teaching_assistant.domain import ModelError, digest
from teaching_assistant.harness import budgets, tool_results
from teaching_assistant.harness.compaction import COMPACTION_VERSION, project_context
from teaching_assistant.harness.context import build_payload, payload
from teaching_assistant.harness.semantic_compaction import (
    SEMANTIC_VERSIONS,
    SummaryRequest,
    accept_context_summary,
    plan_context,
)
from teaching_assistant.harness.store import event


async def _previous(session, run):
    previous = await session.scalar(
        select(AgentContextCheckpoint)
        .where(AgentContextCheckpoint.run_id == run.id)
        .order_by(AgentContextCheckpoint.sequence.desc())
        .limit(1)
    )
    if previous is not None and (
        not isinstance(previous.details, dict)
        or previous.source_hash != previous.details.get("source_hash")
        or digest(previous.messages) != previous.details.get("projection_hash")
    ):
        raise ModelError("agent_context_checkpoint_mismatch", retryable=False)
    return previous


def _options(run, task, previous):
    limit = min(run.limits["max_context_characters"], run.manifest["max_input_characters"])

    def measure(messages):
        body = build_payload(messages, run.manifest, task.registry.schemas())
        size = len(json.dumps(body, ensure_ascii=False, separators=(",", ":")))
        if run.manifest.get("budget"):
            tokens = budgets.input_token_upper_bound(body) + body["max_tokens"]
            window = run.manifest["budget"]["context_window_tokens"]
            # Preserve the character measurement within the token window; when
            # outside, drive the same bounded compression planner before dispatch.
            if tokens > window:
                return max(size, limit + tokens - window)
        return size

    checkpoint = dict(previous.details) if previous is not None else None
    if checkpoint is not None:
        checkpoint.pop("source_view", None)
        if checkpoint["version"] == "tool-result-view-v1":
            checkpoint = None

    return {
        "character_limit": limit,
        "measure": measure,
        "read_tools": {
            tool["name"] for tool in task.registry.manifest() if tool["effect"] == "read"
        },
        "keep_recent_groups": run.manifest.get("context_keep_recent_groups", 2),
        "checkpoint": checkpoint,
    }


async def _source(session, run, previous):
    source = await tool_results.project_messages(session, run)
    if previous is not None and "source_view" in previous.details:
        saved = previous.details["source_view"]
        count = saved.get("source_message_count")
        if type(count) is not int or not 0 < count <= len(run.messages):
            raise ModelError("agent_context_checkpoint_mismatch", retryable=False)
        prefix = await tool_results.project_messages(session, run, run.messages[:count])
        if (
            saved
            != {
                "version": "tool-result-view-v1",
                "source_message_count": count,
                "original_source_hash": prefix.source_hash,
                "references_hash": digest(prefix.references),
            }
            or previous.source_hash != prefix.projection_hash
        ):
            raise ModelError("agent_context_checkpoint_mismatch", retryable=False)
    return source


def _save(session, run, projection, previous, source=None):
    checkpoint = projection.checkpoint
    if source is not None and source.references:
        checkpoint = (
            dict(checkpoint)
            if checkpoint is not None
            else {
                "version": "tool-result-view-v1",
                "source_hash": source.projection_hash,
                "projection_hash": digest(projection.messages),
                "source_message_count": len(run.messages),
            }
        )
        checkpoint["source_view"] = {
            "version": "tool-result-view-v1",
            "source_message_count": len(run.messages),
            "original_source_hash": source.source_hash,
            "references_hash": digest(source.references),
        }
    if checkpoint is not None and (previous is None or previous.details != checkpoint):
        sequence = event(session, run, "context_compacted", **checkpoint)
        session.add(
            AgentContextCheckpoint(
                run_id=run.id,
                sequence=sequence,
                source_hash=checkpoint["source_hash"],
                messages=projection.messages,
                details=checkpoint,
            )
        )


async def prepare_context(session, run, task):
    """Return a bounded payload or a summary request for the same model adapter.

    The caller holds the fenced row lock, reserves the shared model budget, and
    persists SummaryRequest.plan on its model-dispatched event before calling
    the provider. Old manifests retain their original context behavior.
    """
    schemas = task.registry.schemas()
    version = run.manifest.get("context_version")
    if version in (None, "exact-context-v1"):
        return payload(run.messages, run.manifest, run.limits, schemas)
    if version not in (COMPACTION_VERSION, *SEMANTIC_VERSIONS):
        raise ModelError("agent_context_version_unsupported", retryable=False)
    previous = await _previous(session, run)
    source = await _source(session, run, previous)
    options = _options(run, task, previous)
    if version in SEMANTIC_VERSIONS:
        projection = plan_context(
            source.messages,
            policy=run.manifest,
            keep_recent_messages=run.manifest.get("context_keep_recent_messages", 8),
            **options,
        )
        if isinstance(projection, SummaryRequest):
            budgets.request_bounds(projection.body, run.manifest)
            return projection
    else:
        projection = project_context(source.messages, **options)
    _save(session, run, projection, previous, source)
    return payload(projection.messages, run.manifest, run.limits, schemas)


async def accept_summary(session, run, task, plan, reply):
    """Validate and checkpoint a model summary inside the fenced receipt transaction."""
    if run.manifest.get("context_version") not in SEMANTIC_VERSIONS:
        raise ModelError("agent_context_version_unsupported", retryable=False)
    previous = await _previous(session, run)
    source = await _source(session, run, previous)
    projection = accept_context_summary(
        source.messages,
        plan=plan,
        reply=reply,
        policy=run.manifest,
        keep_recent_messages=run.manifest.get("context_keep_recent_messages", 8),
        **_options(run, task, previous),
    )
    _save(session, run, projection, previous, source)
    return projection

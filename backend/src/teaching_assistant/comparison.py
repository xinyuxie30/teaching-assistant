"""Explicit, bounded small-sample comparison. Does not create or confirm business grades."""

import asyncio
import json
import statistics
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from teaching_assistant.domain import (
    AssessmentInput,
    Contract,
    DomainError,
    Evaluation,
    Key,
    ModelError,
    Nonempty,
    Rubric,
    calculate_score,
    candidate,
    digest,
    text_digest,
)
from teaching_assistant.evaluators import build_adapters
from teaching_assistant.profiles import Profile, ProfileRegistry
from teaching_assistant.settings import Settings


class Sample(Contract):
    sample_id: Key
    answer_version_id: UUID
    answer_text: str = Field(max_length=100000)
    gold_levels: dict[str, str] | None = None


class Dataset(Contract):
    schema_version: Literal["grading-comparison-v1"]
    dataset_id: Key
    split: Literal["smoke", "calibration", "holdout"]
    label_status: Literal["synthetic", "teacher_confirmed", "unlabeled"]
    label_source: Nonempty
    question_version_id: UUID
    rubric_version_id: UUID
    question: Nonempty
    reference: Nonempty
    rubric: Rubric
    samples: list[Sample] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_labels(self):
        if len({s.sample_id for s in self.samples}) != len(self.samples):
            raise ValueError("Duplicate sample_id")
        if len({s.answer_version_id for s in self.samples}) != len(self.samples):
            raise ValueError("Duplicate answer_version_id")
        allowed = {c.id: {level.id for level in c.levels} for c in self.rubric.criteria}
        for sample in self.samples:
            gold = sample.gold_levels
            if self.label_status == "unlabeled" and gold is not None:
                raise ValueError("An unlabeled dataset cannot contain gold labels")
            if self.label_status == "teacher_confirmed" and gold is None:
                raise ValueError("Teacher-confirmed samples must all have gold labels")
            if gold is not None and (
                set(gold) != set(allowed)
                or any(level not in allowed[key] for key, level in gold.items())
            ):
                raise ValueError("Gold labels must cover exactly the rubric's valid levels")
        return self


def load_dataset(path: Path) -> Dataset:
    # This budget covers the complete local file, not an inferred model token budget.
    if path.stat().st_size > 10_000_000:
        raise ValueError("Dataset exceeds the 10 MB input limit")
    return Dataset.model_validate_json(path.read_text(encoding="utf-8-sig"))


def numeric_mean(values: list[Decimal]) -> str | None:
    if not values:
        return None
    return str((sum(values, Decimal(0)) / len(values)).quantize(Decimal("0.0001")))


def summarize(report: dict, dataset: Dataset) -> dict:
    point_values = {
        c.id: {level.id: level.points for level in c.levels} for c in dataset.rubric.criteria
    }
    summaries = []
    for profile in report["profiles"]:
        rows = [row for row in report["results"] if row["profile_id"] == profile["id"]]
        labeled = [row for row in rows if row["gold_level_id"] is not None]
        answered = [row for row in labeled if row["status"] == "assessed"]
        mismatches = [row for row in answered if row["candidate_level_id"] != row["gold_level_id"]]
        point_differences = [
            point_values[row["criterion_id"]][row["candidate_level_id"]]
            - point_values[row["criterion_id"]][row["gold_level_id"]]
            for row in answered
        ]
        high_confidence = [
            row
            for row in answered
            if row["evaluation"]["provider_confidence"] is not None
            and row["evaluation"]["provider_confidence"]
            >= report["policy"]["confidence_audit_threshold"]
        ]
        high_errors = [
            {
                "sample_id": row["sample_id"],
                "criterion_id": row["criterion_id"],
                "gold_level_id": row["gold_level_id"],
                "candidate_level_id": row["candidate_level_id"],
                "confidence": row["evaluation"]["provider_confidence"],
            }
            for row in high_confidence
            if row["candidate_level_id"] != row["gold_level_id"]
        ]
        answer_scores, total_differences = [], []
        for sample in dataset.samples:
            points = [row for row in rows if row["sample_id"] == sample.sample_id]
            selected = {
                row["criterion_id"]: row["candidate_level_id"]
                if row["status"] == "assessed"
                else None
                for row in points
            }
            predicted = calculate_score(dataset.rubric, selected)
            gold = (
                calculate_score(dataset.rubric, sample.gold_levels) if sample.gold_levels else None
            )
            difference = predicted - gold if predicted is not None and gold is not None else None
            if difference is not None:
                total_differences.append(difference)
            answer_scores.append(
                {
                    "sample_id": sample.sample_id,
                    "suggested_score": str(predicted) if predicted is not None else None,
                    "gold_score": str(gold) if gold is not None else None,
                    "difference": str(difference) if difference is not None else None,
                }
            )
        attempts = [attempt for row in rows for attempt in row["attempts"]]
        external = [] if profile["manifest"]["simulation"] else attempts
        reported_usage = [
            a["usage"]
            for a in external
            if a["usage"] is not None and {"input_tokens", "output_tokens"} <= set(a["usage"])
        ]
        input_tokens = sum(u["input_tokens"] for u in reported_usage)
        output_tokens = sum(u["output_tokens"] for u in reported_usage)
        unknown_usage = len(external) - len(reported_usage)
        pricing = report["policy"]["pricing"]
        estimated_cost = None
        if pricing is not None and external and reported_usage:
            estimated_cost = str(
                (
                    Decimal(input_tokens) * Decimal(pricing["input_usd_per_million"])
                    + Decimal(output_tokens) * Decimal(pricing["output_usd_per_million"])
                )
                / Decimal(1_000_000)
            )
        durations = [a["duration_ms"] for a in attempts if a["duration_ms"] is not None]
        severe_cutoff = report["policy"]["severe_error_points"]
        summaries.append(
            {
                "profile_id": profile["id"],
                "simulation": profile["manifest"]["simulation"],
                "points_total": len(rows),
                "attempted_points": sum(bool(row["attempts"]) for row in rows),
                "assessed_points": sum(row["status"] == "assessed" for row in rows),
                "abstained_points": sum(row["status"] == "abstained" for row in rows),
                "failed_points": sum(row["status"] == "failed" for row in rows),
                "interrupted_points": sum(row["status"] == "interrupted" for row in rows),
                "budget_skipped_points": sum(row["status"] == "budget_skipped" for row in rows),
                "pending_points": sum(row["status"] == "pending" for row in rows),
                "labeled_points": len(labeled),
                "answered_labeled_points": len(answered),
                "label_coverage": len(answered) / len(labeled) if labeled else None,
                "level_accuracy_on_answered": (
                    (len(answered) - len(mismatches)) / len(answered) if answered else None
                ),
                "criterion_points_mae_on_answered": numeric_mean(
                    [abs(x) for x in point_differences]
                ),
                "criterion_points_bias_on_answered": numeric_mean(point_differences),
                "complete_labeled_answers": len(total_differences),
                "total_points_mae_on_complete": numeric_mean([abs(x) for x in total_differences]),
                "total_points_bias_on_complete": numeric_mean(total_differences),
                "severe_total_error_count": (
                    sum(abs(x) >= Decimal(severe_cutoff) for x in total_differences)
                    if severe_cutoff is not None
                    else None
                ),
                "high_confidence_labeled_points": len(high_confidence),
                "high_confidence_errors": high_errors,
                "answer_scores": answer_scores,
                "adapter_attempts": len(attempts),
                "external_request_attempts": len(external),
                "reported_input_tokens": input_tokens if reported_usage else None,
                "reported_output_tokens": output_tokens if reported_usage else None,
                "external_attempts_with_unknown_usage": unknown_usage,
                "estimated_usd_for_reported_usage": estimated_cost,
                "cost_is_complete": bool(
                    external and reported_usage and unknown_usage == 0 and pricing
                ),
                "attempt_latency_ms_median": statistics.median(durations) if durations else None,
                "attempt_latency_ms_max": max(durations) if durations else None,
                "note": "Small-sample descriptive values; failures and abstentions are excluded from score errors.",
            }
        )
    paired = []
    for index, left in enumerate(report["profiles"]):
        for right in report["profiles"][index + 1 :]:
            left_rows = {
                (r["sample_id"], r["criterion_id"]): r
                for r in report["results"]
                if r["profile_id"] == left["id"] and r["status"] == "assessed"
            }
            right_rows = {
                (r["sample_id"], r["criterion_id"]): r
                for r in report["results"]
                if r["profile_id"] == right["id"] and r["status"] == "assessed"
            }
            common = sorted(left_rows.keys() & right_rows.keys())
            disagreements = [
                {
                    "sample_id": key[0],
                    "criterion_id": key[1],
                    "left_level": left_rows[key]["candidate_level_id"],
                    "right_level": right_rows[key]["candidate_level_id"],
                    "gold_level": left_rows[key]["gold_level_id"],
                }
                for key in common
                if left_rows[key]["candidate_level_id"] != right_rows[key]["candidate_level_id"]
            ]
            paired.append(
                {
                    "left_profile": left["id"],
                    "right_profile": right["id"],
                    "common_assessed_points": len(common),
                    "disagreements": disagreements,
                }
            )
    return {"profiles": summaries, "paired": paired}


def write_report(directory: Path, report: dict, dataset: Dataset) -> None:
    report["summary"] = summarize(report, dataset)
    target = directory / "report.json"
    temporary = directory / "report.json.tmp"
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)


async def persist_report(directory: Path, report: dict, dataset: Dataset) -> None:
    # A cancelled to_thread call keeps running. Finish that write before the final interrupted report.
    writing = asyncio.create_task(asyncio.to_thread(write_report, directory, report, dataset))
    try:
        await asyncio.shield(writing)
    except asyncio.CancelledError:
        await writing
        raise


def prepare_output(directory: Path, dataset: Dataset) -> None:
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "dataset.snapshot.json").write_text(
        dataset.model_dump_json(indent=2), encoding="utf-8"
    )


def validate_options(args) -> dict | None:
    if not 1 <= args.max_samples <= 200 or not 1 <= args.max_calls <= 10000:
        raise ValueError("max-samples must be 1–200 and max-calls must be 1–10000")
    if not 1 <= args.max_seconds <= 3600 or not 1 <= args.max_attempts <= 3:
        raise ValueError("max-seconds must be 1–3600 and max-attempts must be 1–3")
    if not 0 <= args.confidence_audit_threshold <= 1:
        raise ValueError("confidence-audit-threshold must be in [0, 1]")
    if args.severe_error_points is not None and (
        not args.severe_error_points.is_finite() or args.severe_error_points <= 0
    ):
        raise ValueError("severe-error-points must be a finite positive number")
    prices = (args.input_usd_per_million, args.output_usd_per_million)
    if all(p is None for p in prices):
        return None
    if any(p is None or not p.is_finite() or p < 0 for p in prices) or not args.price_source:
        raise ValueError("Specify both finite nonnegative token prices and --price-source")
    return {
        "input_usd_per_million": str(prices[0]),
        "output_usd_per_million": str(prices[1]),
        "source": args.price_source,
    }


async def run_comparison(settings: Settings, args) -> dict:
    pricing = validate_options(args)
    path = Path(args.dataset)
    original = await asyncio.to_thread(load_dataset, path)
    dataset = original.model_copy(update={"samples": original.samples[: args.max_samples]})
    registry = ProfileRegistry(settings)
    profiles = [registry.resolve(name, require_ready=not args.dry_run) for name in args.profiles]
    if len({profile.id for profile in profiles}) != len(profiles):
        raise ValueError("Do not select the same profile or alias twice")
    if len(profiles) < 1 or len(profiles) > 4:
        raise ValueError("Select 1–4 distinct profiles")
    planned_points = len(dataset.samples) * len(dataset.rubric.criteria) * len(profiles)
    plan = {
        "dataset_id": dataset.dataset_id,
        "label_status": dataset.label_status,
        "selected_samples": len(dataset.samples),
        "available_samples": len(original.samples),
        "profiles": [
            {"id": p.id, "manifest": p.manifest, "ready": registry.is_ready(p)} for p in profiles
        ],
        "planned_adapter_calls_without_retries": planned_points,
        "planned_external_calls_without_retries": (
            len(dataset.samples)
            * len(dataset.rubric.criteria)
            * sum(not p.manifest["simulation"] for p in profiles)
        ),
        "max_adapter_calls": args.max_calls,
        "max_seconds": args.max_seconds,
        "max_attempts_per_point": args.max_attempts,
    }
    if args.dry_run:
        return {
            "dry_run": True,
            "plan": plan,
            "notice": "No database writes or model requests made.",
        }

    run_id = uuid4().hex
    directory = Path(args.output_dir) / run_id
    await asyncio.to_thread(prepare_output, directory, dataset)
    report = {
        "schema_version": "grading-comparison-report-v1",
        "run_id": run_id,
        "started_at": datetime.now(UTC).isoformat(),
        "status": "running",
        "evaluation_scope": "criterion_assessment_only",
        "dataset_id": dataset.dataset_id,
        "dataset_hash": digest(dataset.model_dump(mode="json")),
        "question_hash": digest({"question": dataset.question, "reference": dataset.reference}),
        "rubric_hash": digest(dataset.rubric.model_dump(mode="json")),
        "split": dataset.split,
        "label_status": dataset.label_status,
        "label_source": dataset.label_source,
        "plan": plan,
        "profiles": plan["profiles"],
        "policy": {
            "confidence_audit_threshold": args.confidence_audit_threshold,
            "severe_error_points": str(args.severe_error_points)
            if args.severe_error_points is not None
            else None,
            "pricing": pricing,
            "max_attempts_per_point": args.max_attempts,
        },
        "limitations": [
            "fake-v1 is a plumbing control, not an ordinary-language-model quality baseline.",
            "No automatic evidence extraction or teacher review time is measured.",
            "Confidence is not correctness; the audit threshold never auto-confirms a grade.",
            "Synthetic labels do not establish teaching quality; use teacher-confirmed held-out samples.",
            "No result from this command is written as a student grade.",
        ],
        "results": [],
    }
    adapters = {p.id: build_adapters(settings, p)[0] for p in profiles}
    requests: list[tuple[dict, AssessmentInput, Profile]] = []
    for sample in dataset.samples:
        for criterion in dataset.rubric.criteria:
            for profile in profiles:
                request = AssessmentInput(
                    question_version_id=dataset.question_version_id,
                    answer_version_id=sample.answer_version_id,
                    rubric_version_id=dataset.rubric_version_id,
                    question=dataset.question,
                    reference=dataset.reference,
                    answer=sample.answer_text,
                    global_instructions=dataset.rubric.global_instructions,
                    criterion=criterion,
                    profile=profile.id,
                )
                row = {
                    "sample_id": sample.sample_id,
                    "criterion_id": criterion.id,
                    "profile_id": profile.id,
                    "answer_hash": text_digest(sample.answer_text),
                    "input_hash": digest(request.model_dump(mode="json")),
                    "gold_level_id": sample.gold_levels[criterion.id]
                    if sample.gold_levels
                    else None,
                    "candidate_level_id": None,
                    "status": "pending",
                    "error_code": None,
                    "evaluation": None,
                    "attempts": [],
                    "prompt_hash": None,
                }
                report["results"].append(row)
                # Reject an oversized request before reserving a potentially billable call.
                try:
                    prepare = getattr(adapters[profile.id], "prepare", None)
                    if callable(prepare):
                        prepare(request)
                    prompt_hash = getattr(adapters[profile.id], "prompt_hash", None)
                    if callable(prompt_hash):
                        row["prompt_hash"] = prompt_hash(request)
                except ModelError as exc:
                    row["status"], row["error_code"] = "failed", exc.code
                else:
                    requests.append((row, request, profile))
    deadline = time.monotonic() + args.max_seconds
    calls_used = 0
    try:
        await persist_report(directory, report, dataset)
        for row, request, profile in requests:
            adapter = adapters[profile.id]
            for attempt_index in range(args.max_attempts):
                remaining = deadline - time.monotonic()
                if calls_used >= args.max_calls or remaining <= 0:
                    if not row["attempts"]:
                        row["status"] = "budget_skipped"
                    row["error_code"] = "comparison_budget_exhausted"
                    break
                calls_used += 1
                attempt = {
                    "attempt": attempt_index + 1,
                    "requested_model": profile.manifest["model"],
                    "returned_model": None,
                    "provider_request_id": None,
                    "prompt_hash": row["prompt_hash"],
                    "usage": None,
                    "http_status": None,
                    "status": "dispatched",
                    "error_code": None,
                    "duration_ms": None,
                }
                row["attempts"].append(attempt)
                # Persist the reserved call before sending, so interrupted runs expose uncertainty.
                await persist_report(directory, report, dataset)
                started = time.monotonic()
                output, failure = None, None
                try:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ModelError("comparison_budget_exhausted", retryable=False)
                    async with asyncio.timeout(min(remaining, settings.call_timeout_seconds)):
                        output = Evaluation.model_validate(await adapter.assess(request))
                        level = candidate(output, request.criterion)
                    row["candidate_level_id"] = level
                    row["evaluation"] = output.model_dump(mode="json")
                    row["status"] = "assessed" if level is not None else "abstained"
                    row["error_code"] = None
                    attempt.update(
                        {
                            "status": "succeeded",
                            "returned_model": output.returned_model,
                            "provider_request_id": output.provider_request_id,
                            "prompt_hash": output.prompt_hash or attempt["prompt_hash"],
                            "usage": output.usage,
                            "http_status": output.http_status,
                        }
                    )
                except asyncio.CancelledError:
                    attempt["status"], attempt["error_code"] = "unknown", "comparison_interrupted"
                    raise
                except Exception as exc:
                    code = (
                        exc.code
                        if isinstance(exc, DomainError)
                        else "model_timeout"
                        if isinstance(exc, TimeoutError)
                        else "adapter_error"
                    )
                    failure = (
                        exc if isinstance(exc, ModelError) else ModelError(code, retryable=True)
                    )
                    row["status"], row["error_code"] = "failed", code
                    attempt.update(
                        {
                            "status": "unknown"
                            if code
                            in {"model_timeout", "provider_timeout", "provider_transport_error"}
                            else "failed",
                            "error_code": code,
                            "usage": failure.usage,
                            "returned_model": failure.returned_model,
                            "provider_request_id": failure.provider_request_id,
                            "http_status": failure.http_status,
                        }
                    )
                    if output is not None:
                        attempt["usage"] = output.usage
                finally:
                    attempt["duration_ms"] = int((time.monotonic() - started) * 1000)
                    report["calls_used"] = calls_used
                    await persist_report(directory, report, dataset)
                if (
                    failure is None
                    or not failure.retryable
                    or attempt_index + 1 == args.max_attempts
                ):
                    break
                delay = max(
                    settings.retry_delay_seconds * 2**attempt_index,
                    failure.retry_after_seconds or 0,
                )
                if calls_used >= args.max_calls or time.monotonic() + delay >= deadline:
                    break
                await asyncio.sleep(delay)
        report["status"] = "finished"
    except asyncio.CancelledError:
        report["status"] = "interrupted"
        raise
    except Exception:
        report["status"] = "failed"
        raise
    finally:
        # A reservation persisted immediately before cancellation may not have reached the provider.
        # Keep that uncertainty visible; never convert an unknown response into a zero score.
        for row in report["results"]:
            for attempt in row["attempts"]:
                if attempt["status"] == "dispatched":
                    attempt["status"], attempt["error_code"] = "unknown", "comparison_interrupted"
            if row["status"] == "pending" and row["attempts"]:
                row["status"], row["error_code"] = "interrupted", "comparison_interrupted"
        report["calls_used"] = calls_used
        report["finished_at"] = datetime.now(UTC).isoformat()
        await persist_report(directory, report, dataset)
    return {
        "run_id": run_id,
        "status": report["status"],
        "report": str(directory / "report.json"),
        "dataset_snapshot": str(directory / "dataset.snapshot.json"),
        "summary": report["summary"],
    }

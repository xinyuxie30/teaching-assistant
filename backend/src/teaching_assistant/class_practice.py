"""Class practice retrieval from confirmed reviews, confirmed memory and a private bank.

Snapshots contain aggregate evidence only. Original grading never consumes this history.
"""

import re
from datetime import timedelta
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StringConstraints, model_validator
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from teaching_assistant import analysis, service, teaching_memory
from teaching_assistant.db import (
    AgentRun,
    Assignment,
    Classroom,
    GradingBatch,
    PracticeQuestion,
    QuestionVersion,
    RubricVersion,
)
from teaching_assistant.domain import Contract, DomainError, digest
from teaching_assistant.listings import page
from teaching_assistant.schemas import json_time

Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
Hash = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]


class QuestionInput(Contract):
    title: Label
    prompt: Text
    expected_answer: Text
    knowledge_points: list[Label] = Field(min_length=1, max_length=12)
    grade: Label
    difficulty: Literal["basic", "standard", "advanced"] = "standard"
    question_type: Label = "简答题"
    source: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]

    @model_validator(mode="after")
    def unique_tags(self):
        if len({s.casefold() for s in self.knowledge_points}) != len(self.knowledge_points):
            raise ValueError("Knowledge points must be distinct")
        return self


class BankImport(Contract):
    questions: list[QuestionInput] = Field(min_length=1, max_length=100)


class HashInput(Contract):
    content_hash: Hash


class PracticeRequest(Contract):
    batch_ids: list[UUID] = Field(min_length=1, max_length=10)
    knowledge_points: list[Label] = Field(default_factory=list, max_length=8)
    grade: Label | None = None
    difficulty: Literal["basic", "standard", "advanced"] | None = None
    question_type: Label | None = None
    memory_query: str = Field(default="", max_length=200)
    question_count: int = Field(default=3, ge=1, le=5, strict=True)
    instructions: Text = "根据本班已确认的历史评分点表现，选择补练题并说明安排。"

    @model_validator(mode="after")
    def distinct(self):
        if len(set(self.batch_ids)) != len(self.batch_ids):
            raise ValueError("Select distinct batches")
        return self


class PracticeRunInput(PracticeRequest):
    preview_hash: Hash
    selected_question_ids: list[UUID] = Field(default_factory=list, max_length=5)
    agent_profile: Literal["fake-class-practice-v1", "deepseek-class-practice-v1"] = (
        "fake-class-practice-v1"
    )

    @model_validator(mode="after")
    def selected_count(self):
        if self.selected_question_ids and (
            len(set(self.selected_question_ids)) != len(self.selected_question_ids)
            or len(self.selected_question_ids) != self.question_count
        ):
            raise ValueError("Select exactly question_count distinct candidates or leave empty")
        return self


async def owned_classroom(session, teacher, classroom_id):
    row = await session.get(Classroom, classroom_id)
    if row is None:
        raise DomainError("not_found", "Classroom not found", 404)
    await service.owned_course(session, teacher, row.course_id)
    return row


def bank_view(row):
    if digest(row.body) != row.content_hash:
        raise DomainError("question_content_mismatch", "Question failed integrity check", 409)
    return {
        "id": str(row.id),
        "course_id": str(row.course_id),
        **row.body,
        "content_hash": row.content_hash,
        "created_at": json_time(row.created_at),
        "confirmed_at": json_time(row.confirmed_at),
        "retired_at": json_time(row.retired_at),
    }


async def import_bank(session, teacher, course_id, data):
    await service.owned_course(session, teacher, course_id)
    result = []
    for question in data.questions:
        body = question.model_dump(mode="json")
        content_hash = digest(body)
        await session.execute(
            insert(PracticeQuestion)
            .values(
                teacher_id=teacher,
                course_id=course_id,
                body=body,
                content_hash=content_hash,
            )
            .on_conflict_do_nothing(index_elements=["course_id", "content_hash"])
        )
        row = await session.scalar(
            select(PracticeQuestion).where(
                PracticeQuestion.course_id == course_id,
                PracticeQuestion.content_hash == content_hash,
            )
        )
        result.append(bank_view(row))
    service.audit(session, teacher, "practice_bank_imported", course_id, count=len(result))
    return {"items": result}


async def bank_state(session, teacher, course_id, question_id, data, *, retire=False):
    await service.owned_course(session, teacher, course_id)
    row = await session.scalar(
        select(PracticeQuestion)
        .where(
            PracticeQuestion.id == question_id,
            PracticeQuestion.teacher_id == teacher,
            PracticeQuestion.course_id == course_id,
        )
        .with_for_update()
    )
    if row is None:
        raise DomainError("not_found", "Question not found", 404)
    bank_view(row)
    if row.content_hash != data.content_hash:
        raise DomainError("question_changed", "Read the question before confirming", 409)
    if not retire and row.retired_at is not None:
        raise DomainError(
            "question_retired", "Import a new version to replace retired content", 409
        )
    field = "retired_at" if retire else "confirmed_at"
    if getattr(row, field) is None:
        setattr(row, field, await session.scalar(select(func.clock_timestamp())))
        service.audit(session, teacher, "practice_question_" + field, row.id)
    return bank_view(row)


async def list_bank(session, teacher, course_id, params):
    await service.owned_course(session, teacher, course_id)
    query = select(PracticeQuestion.id, PracticeQuestion.created_at).where(
        PracticeQuestion.teacher_id == teacher,
        PracticeQuestion.course_id == course_id,
    )
    result = await page(
        session, query, PracticeQuestion, teacher, f"practice-bank:{course_id}", params
    )
    ids = [UUID(item["id"]) for item in result["items"]]
    rows = {
        str(row.id): row
        for row in await session.scalars(
            select(PracticeQuestion).where(PracticeQuestion.id.in_(ids))
        )
    }
    result["items"] = [bank_view(rows[item["id"]]) for item in result["items"]]
    return result


async def list_batches(session, teacher, classroom_id, params):
    classroom = await owned_classroom(session, teacher, classroom_id)
    query = (
        select(
            GradingBatch.id,
            GradingBatch.created_at,
            Assignment.title.label("assignment_title"),
            QuestionVersion.question_key,
            QuestionVersion.version.label("question_version"),
            GradingBatch.status,
        )
        .join(RubricVersion, RubricVersion.id == GradingBatch.rubric_version_id)
        .join(QuestionVersion, QuestionVersion.id == RubricVersion.question_version_id)
        .join(Assignment, Assignment.id == QuestionVersion.assignment_id)
        .where(GradingBatch.teacher_id == teacher, Assignment.classroom_id == classroom.id)
    )
    return await page(
        session, query, GradingBatch, teacher, f"practice-batches:{classroom_id}", params
    )


def terms(value):
    """Auditable lexical retrieval, including Chinese bigrams; not semantic embeddings."""
    result = set()
    for part in re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]+", value.casefold()):
        if re.fullmatch(r"[\u3400-\u9fff]+", part):
            result.update(part[i : i + 2] for i in range(len(part) - 1))
        elif len(part) > 1:
            result.add(part)
    return result


def rank_question(question, evidence, requested_topics, memories):
    searchable = " ".join([question["title"], question["prompt"], *question["knowledge_points"]])
    tokens = terms(searchable)
    matches = []
    score = 0
    for item in evidence:
        overlap = sorted(tokens & terms(item["description"]))
        if overlap:
            matches.append({"evidence_id": item["id"], "terms": overlap[:8]})
            score += (
                min(len(overlap), 8)
                + item["below_full_credit_students"] / item["confirmed_students"]
            )
    topic_matches = [tag for tag in requested_topics if tag.casefold() in searchable.casefold()]
    score += 10 * len(topic_matches)
    # Memory may refine ordering, but cannot alone justify a history-targeted recommendation.
    if not matches and not topic_matches:
        return None
    memory_matches = [m["id"] for m in memories if tokens & terms(m["title"] + " " + m["body"])]
    return {
        "question": question,
        "score": round(score + min(len(memory_matches), 3) * 0.25, 4),
        "evidence_matches": matches,
        "topic_matches": topic_matches,
        "memory_ids": memory_matches,
    }


async def preview(session, teacher, classroom_id, data: PracticeRequest):
    classroom = await owned_classroom(session, teacher, classroom_id)
    evidence, batches, seen = [], [], set()
    for batch_id in sorted(data.batch_ids, key=str):
        report = await analysis.batch_analysis(session, teacher, batch_id)
        if report["scope"]["classroom_id"] != str(classroom.id):
            raise DomainError(
                "practice_scope_mismatch", "All batches must belong to this classroom"
            )
        identity = (report["scope"]["assignment_id"], report["scope"]["question_key"])
        if identity in seen:
            raise DomainError(
                "practice_duplicate_question", "Choose one batch per assignment question"
            )
        seen.add(identity)
        batches.append(
            {key: report[key] for key in ("batch_id", "basis_hash", "scope", "coverage")}
        )
        for criterion in report["criteria"]:
            if criterion["confirmed_students"] and criterion["below_full_credit_students"]:
                evidence.append(
                    {
                        "id": f"{batch_id}:{criterion['id']}",
                        "batch_id": str(batch_id),
                        **{
                            key: criterion[key]
                            for key in (
                                "description",
                                "confirmed_students",
                                "below_full_credit_students",
                                "below_full_credit_rate",
                                "score_rate",
                                "levels",
                            )
                        },
                    }
                )
    if not evidence:
        raise DomainError(
            "practice_no_evidence",
            "No confirmed below-full-credit evidence in selected batches",
            409,
        )
    memory = await teaching_memory.search_memories(
        session, teacher, classroom.course_id, data.memory_query
    )
    statement = select(PracticeQuestion).where(
        PracticeQuestion.teacher_id == teacher,
        PracticeQuestion.course_id == classroom.course_id,
        PracticeQuestion.confirmed_at.is_not(None),
        PracticeQuestion.retired_at.is_(None),
    )
    for field in ("grade", "difficulty", "question_type"):
        if getattr(data, field):
            statement = statement.where(PracticeQuestion.body[field].astext == getattr(data, field))
    rows = list(await session.scalars(statement.order_by(PracticeQuestion.id).limit(2001)))
    if len(rows) > 2000:
        raise DomainError(
            "practice_bank_too_broad", "Narrow the grade, difficulty or question type filters"
        )
    ranked = [
        result
        for row in rows
        if (
            result := rank_question(
                bank_view(row),
                evidence,
                data.knowledge_points,
                memory["items"],
            )
        )
        is not None
    ]
    ranked.sort(key=lambda item: (-item["score"], item["question"]["id"]))
    fixed = {
        "version": "class-practice-retrieval-v1",
        "course_id": str(classroom.course_id),
        "classroom_id": str(classroom.id),
        "classroom_name": classroom.name,
        "request": PracticeRequest.model_validate(
            data.model_dump(include=set(PracticeRequest.model_fields))
        ).model_dump(mode="json"),
        "batches": batches,
        "evidence": evidence,
        "memories": memory["items"],
        "candidates": ranked[:20],
        "matching_count": len(ranked),
    }
    return {**fixed, "preview_hash": digest(fixed)}


async def create_run(session, teacher, classroom_id, data, settings):
    from teaching_assistant.agent import service as agents
    from teaching_assistant.harness import budgets, catalog
    from teaching_assistant.harness.context import initial_messages, payload

    snapshot = await preview(session, teacher, classroom_id, data)
    if snapshot["preview_hash"] != data.preview_hash:
        raise DomainError("practice_preview_changed", "Sources changed; preview again", 409)
    candidates = snapshot["candidates"]
    if len(candidates) < data.question_count:
        raise DomainError(
            "practice_not_enough_questions",
            "Add matching confirmed bank questions or lower the count",
            409,
        )
    if data.selected_question_ids:
        selected_ids = set(map(str, data.selected_question_ids))
        candidates = [c for c in candidates if c["question"]["id"] in selected_ids]
        if len(candidates) != len(selected_ids):
            raise DomainError(
                "practice_invalid_selection", "Selection must come from the current preview"
            )
    snapshot["candidates"] = candidates
    snapshot["snapshot_hash"] = digest(snapshot)
    task = catalog.resolve("class-practice")
    profile = next(
        p
        for p in catalog.profiles(settings, task_type=task.key)
        if p["alias"] == data.agent_profile
    )
    if not profile["ready"]:
        raise DomainError("model_not_configured", "Configure selected model first", 409)
    limits = {
        "max_model_calls": 6,
        "max_tool_calls": 8,
        "max_context_characters": 100000,
        "max_seconds": 300,
    }
    budgets.validate_limits(limits, profile["manifest"])
    messages = initial_messages(task.system, data.instructions)
    payload(messages, profile["manifest"], limits, task.registry.schemas())
    # Include the future tool result in preflight, before a paid model can be dispatched.
    from teaching_assistant.harness.context import tool_result

    payload(
        [*messages, tool_result("preflight", {"ok": True, "result": snapshot})],
        profile["manifest"],
        limits,
        task.registry.schemas(),
    )
    now = await session.scalar(select(func.clock_timestamp()))
    run = AgentRun(
        teacher_id=teacher,
        task_type=task.key,
        task_input=snapshot,
        profile_id=profile["id"],
        manifest=profile["manifest"],
        limits=limits,
        messages=messages,
        expires_at=now + timedelta(seconds=300),
    )
    session.add(run)
    await session.flush()
    agents.event(session, run, "created", task_type=task.key, input_hash=digest(snapshot))
    service.audit(
        session, teacher, "class_practice_created", run.id, classroom_id=str(classroom_id)
    )
    return agents.view(run)


async def list_runs(session, teacher, classroom_id, params):
    from teaching_assistant.agent import service as agents

    await owned_classroom(session, teacher, classroom_id)
    query = select(AgentRun.id, AgentRun.created_at).where(
        AgentRun.teacher_id == teacher,
        AgentRun.task_type == "class-practice",
        AgentRun.task_input["classroom_id"].astext == str(classroom_id),
    )
    result = await page(session, query, AgentRun, teacher, f"class-practice:{classroom_id}", params)
    ids = [UUID(item["id"]) for item in result["items"]]
    rows = {
        str(row.id): row
        for row in await session.scalars(select(AgentRun).where(AgentRun.id.in_(ids)))
    }
    result["items"] = [agents.view(rows[item["id"]]) for item in result["items"]]
    return result

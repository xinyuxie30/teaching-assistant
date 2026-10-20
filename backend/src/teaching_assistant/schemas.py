from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from teaching_assistant.domain import Contract, Evidence, Key, Nonempty, Points, Rubric


class StudentInput(Contract):
    external_id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=120)


class WorkspaceInput(Contract):
    course_name: str = Field(min_length=1, max_length=200)
    classroom_name: str = Field(min_length=1, max_length=200)
    students: list[StudentInput] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def unique_students(self):
        if len({s.external_id for s in self.students}) != len(self.students):
            raise ValueError("Duplicate student external_id")
        return self


class AssignmentInput(Contract):
    classroom_id: UUID
    title: str = Field(min_length=1, max_length=200)


class QuestionInput(Contract):
    question_key: Key
    prompt: Nonempty
    reference: Nonempty
    maximum_points: Points = Field(gt=0)


class DraftInput(Contract):
    rubric: Rubric


class ConfirmInput(Contract):
    expected_revision: int = Field(ge=1)


class AnswerInput(Contract):
    student_id: UUID
    question_version_id: UUID
    external_submission_id: str = Field(min_length=1, max_length=120)
    source_revision: str = Field(min_length=1, max_length=80)
    # Deliberately preserve all whitespace, line endings and Unicode.
    answer_text: str = Field(max_length=100000)
    submitted_at: AwareDatetime | None = None


class ImportInput(Contract):
    rows: list[AnswerInput] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def unique_rows(self):
        keys = [
            (r.student_id, r.question_version_id, r.external_submission_id, r.source_revision)
            for r in self.rows
        ]
        if len(set(keys)) != len(keys):
            raise ValueError("Duplicate source identity/revision in import")
        if sum(len(r.answer_text) for r in self.rows) > 2_000_000:
            raise ValueError("Import exceeds the 2,000,000 character budget")
        return self


class BatchInput(Contract):
    rubric_version_id: UUID
    answer_version_ids: list[UUID] = Field(min_length=1, max_length=200)
    evaluation_profile: str = Field(default="fake-v1", min_length=1, max_length=80)

    @model_validator(mode="after")
    def unique_answers(self):
        if len(set(self.answer_version_ids)) != len(self.answer_version_ids):
            raise ValueError("Duplicate answer version")
        return self


class ReviewDecision(Contract):
    criterion_id: Key
    level_id: Key
    evidence_support: Literal["supported", "absence_confirmed", "teacher_judgment"]
    reason: Nonempty
    citations: list[Evidence] | None = Field(default=None, max_length=20)


class ReviewInput(Contract):
    expected_revision: int = Field(ge=0)
    decisions: list[ReviewDecision] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def unique_decisions(self):
        if len({d.criterion_id for d in self.decisions}) != len(self.decisions):
            raise ValueError("Duplicate criterion decision")
        return self


class RetryInput(Contract):
    reason: Nonempty
    additional_attempts: int = Field(default=1, ge=1, le=3)
    additional_calls: int = Field(default=2, ge=0, le=60)


class ExportInput(Contract):
    mode: Literal["working", "confirmed_only"] = "working"


def json_time(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class PageQuery(Contract):
    limit: int = Field(default=50, ge=1, le=100)
    cursor: str | None = Field(default=None, max_length=2048)
    created_from: AwareDatetime | None = None
    created_to: AwareDatetime | None = None

    @model_validator(mode="after")
    def valid_period(self):
        if self.created_from and self.created_to and self.created_from > self.created_to:
            raise ValueError("created_from must not be after created_to")
        return self


class SearchQuery(PageQuery):
    q: str | None = Field(default=None, min_length=1, max_length=120)


class StudentQuery(SearchQuery):
    classroom_id: UUID | None = None


class AssignmentQuery(SearchQuery):
    classroom_id: UUID | None = None


class QuestionQuery(PageQuery):
    question_key: Key | None = None


class DraftQuery(PageQuery):
    status: Literal["draft", "confirmed"] | None = None


class AnswerQuery(PageQuery):
    question_version_id: UUID | None = None
    student_id: UUID | None = None
    source_revision: str | None = Field(default=None, min_length=1, max_length=80)


class BatchQuery(PageQuery):
    course_id: UUID | None = None
    assignment_id: UUID | None = None
    rubric_version_id: UUID | None = None
    status: Literal["queued", "running", "settled"] | None = None
    outcome: Literal["all_succeeded", "partial_failure", "all_failed"] | None = None


class ItemQuery(PageQuery):
    course_id: UUID | None = None
    assignment_id: UUID | None = None
    batch_id: UUID | None = None
    rubric_version_id: UUID | None = None
    student_id: UUID | None = None
    status: Literal["pending", "running", "retry_wait", "succeeded", "failed"] | None = None
    review_status: Literal["pending", "confirmed"] | None = None
    evaluation_profile: str | None = Field(default=None, min_length=1, max_length=80)
    issue_code: str | None = Field(default=None, min_length=1, max_length=120)


class ExportQuery(PageQuery):
    batch_id: UUID | None = None
    mode: Literal["working", "confirmed_only"] | None = None

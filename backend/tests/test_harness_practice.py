"""Contracts that keep child results unconfirmed and bound to the delegated brief."""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError
from teaching_assistant.domain import DomainError, digest
from teaching_assistant.harness.context import tool_result
from teaching_assistant.harness.contracts import ToolCall
from teaching_assistant.harness.lesson import FakeLessonModel, LessonPlan
from teaching_assistant.harness.practice import PracticeTask, propose_practice
from teaching_assistant.harness.rubric_model import FakeAgentModel
from teaching_assistant.harness.team_lesson import (
    ChildArtifactResult,
    FakeTeamLessonModel,
    PracticeRequest,
    TeamLessonTask,
    propose_team_lesson_plan,
    request_practice_agent,
)

BRIEF = {
    "topic": "版本记录",
    "objectives": ["解释版本记录", "用例子说明可追溯性"],
    "duration_minutes": 10,
}
PRACTICE = {"prompt": "说明两个版本为什么不能混用", "expected_answer": "先定位相同版本再比较"}


def run_with_child():
    body = {"practice": PRACTICE, "objectives": BRIEF["objectives"]}
    child = {
        "child_run_id": str(uuid4()),
        "artifact_id": str(uuid4()),
        "content_hash": digest(body),
        "task_type": "lesson-practice",
        "body": body,
        "confirmed": False,
    }
    return SimpleNamespace(
        task_input={"course_id": str(uuid4()), "brief": BRIEF},
        task_state={"brief_read": True, "practice_child": child},
        messages=[{"role": "user", "content": "A parent-only conversation detail"}],
    )


@pytest.mark.parametrize(
    "extra", [{"teacher_id": str(uuid4())}, {"task_type": "rubric"}, {"max_depth": 100}]
)
def test_delegation_input_cannot_choose_scope_capabilities_or_budget(extra):
    with pytest.raises(ValidationError):
        PracticeRequest.model_validate({"instructions": "设计一道练习", **extra})


def test_child_cannot_delegate_or_confirm_and_parent_cannot_skip_child():
    for name in ("request_practice_agent", "confirm_lesson_plan", "read_question"):
        call = ToolCall.model_validate(
            {"id": "not-allowed", "function": {"name": name, "arguments": "{}"}}
        )
        with pytest.raises(DomainError) as failure:
            PracticeTask.registry.validate_call(call)
        assert failure.value.code == "tool_not_allowed"
    parent = run_with_child()
    assert (
        TeamLessonTask().pre_tool(parent, "request_practice_agent")
        == "practice_child_already_received"
    )
    parent.task_state = {"brief_read": True}
    assert TeamLessonTask().pre_tool(parent, "propose_lesson_plan") == "practice_agent_required"
    parent.task_state = {}
    assert (
        TeamLessonTask().pre_tool(parent, "request_practice_agent") == "read_lesson_brief_required"
    )


async def test_child_request_copies_only_fixed_scope_not_parent_transcript():
    parent = run_with_child()
    request = await request_practice_agent(
        None, parent, PracticeRequest(instructions="设计一道能检查理解程度的练习")
    )
    assert request.task_type == "lesson-practice"
    assert request.task_input == parent.task_input
    assert request.task_input is not parent.task_input
    assert "A parent-only" not in json.dumps(request.task_input)
    assert request.instructions == "设计一道能检查理解程度的练习"


async def test_child_cannot_save_changed_objectives():
    parent = run_with_child()
    arguments = PracticeTask.registry.validate_call(
        ToolCall.model_validate(
            {
                "id": "wrong-objectives",
                "function": {
                    "name": "propose_practice",
                    "arguments": json.dumps({"practice": PRACTICE, "objectives": ["替换目标"]}),
                },
            }
        )
    )
    with pytest.raises(DomainError) as failure:
        await propose_practice(None, parent, arguments)
    assert failure.value.code == "practice_objectives_mismatch"


@pytest.mark.parametrize("change", ["hash", "objectives"])
async def test_parent_rejects_unverifiable_handoff_before_updating_state(change):
    parent = run_with_child()
    child = dict(parent.task_state["practice_child"])
    parent.task_state = {"brief_read": True}
    if change == "hash":
        child["content_hash"] = "0" * 64
    else:
        child["body"] = {"practice": PRACTICE, "objectives": ["替换目标"]}
        child["content_hash"] = digest(child["body"])
    with pytest.raises(DomainError) as failure:
        await TeamLessonTask().apply_result(None, parent, "request_practice_agent", child)
    assert failure.value.code == (
        "invalid_child_result" if change == "hash" else "practice_objectives_mismatch"
    )
    assert parent.task_state == {"brief_read": True}


def test_child_handoff_never_claims_teacher_confirmation():
    child = run_with_child().task_state["practice_child"]
    with pytest.raises(ValidationError):
        ChildArtifactResult.model_validate({**child, "confirmed": True})


async def test_parent_cannot_fake_using_child_practice():
    parent = run_with_child()
    plan = LessonPlan.model_validate(
        {
            "title": "方案",
            "objectives": BRIEF["objectives"],
            "sections": [{"title": "活动", "minutes": 10, "activity": "讲解并练习"}],
            "practice": {"prompt": "忽略子任务另写的练习", "expected_answer": "替换答案"},
        }
    )
    with pytest.raises(DomainError) as failure:
        await propose_team_lesson_plan(None, parent, plan)
    assert failure.value.code == "practice_handoff_mismatch"


async def test_fake_parent_incorporates_the_actual_handoff():
    child = run_with_child().task_state["practice_child"]
    body = {
        "messages": [
            tool_result("brief", {"ok": True, "result": {"brief": BRIEF}}),
            tool_result("child", {"ok": True, "result": child}),
        ]
    }
    reply = await FakeTeamLessonModel().respond(body, {"model": "fake-lesson-team-agent-v1"})
    call = reply.tool_calls[0]
    assert call.function.name == "propose_lesson_plan"
    assert json.loads(call.function.arguments)["practice"] == PRACTICE


@pytest.mark.parametrize(
    ("model", "policy"),
    [
        (FakeAgentModel(), {"model": "fake-rubric-agent-v1"}),
        (FakeLessonModel(), {"model": "fake-lesson-agent-v1"}),
    ],
)
async def test_fake_call_ids_do_not_collide_after_context_projection(model, policy):
    # A compressed projection may have the same number of messages as an older
    # dispatch while preserving different content. Call IDs still must differ.
    first = {"messages": [{"role": "user", "content": "Original task context"}]}
    second = {"messages": [{"role": "user", "content": "Different projected context"}]}
    left = await model.respond(first, policy)
    right = await model.respond(second, policy)
    assert left.tool_calls[0].id != right.tool_calls[0].id

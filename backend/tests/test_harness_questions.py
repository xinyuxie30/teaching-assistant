"""Teacher questions preserve tool pairing across waits, retries, and process restarts."""

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from conftest import post
from pydantic import ValidationError
from sqlalchemy import func, select
from teaching_assistant.api import create_app
from teaching_assistant.cli import bootstrap
from teaching_assistant.db import AgentArtifact, AgentRun, AgentWait, AgentWake
from teaching_assistant.domain import DomainError, digest
from teaching_assistant.harness import catalog, questions, scheduler, waiting
from teaching_assistant.harness.context import tool_result
from teaching_assistant.harness.contracts import Reply, ToolCall
from teaching_assistant.harness.worker import HarnessWorker


@pytest.mark.parametrize(
    "arguments",
    [
        {"prompt": " "},
        {"prompt": "x" * 1001},
        {"prompt": "Question?", "options": []},
        {"prompt": "Question?", "options": ["One"]},
        {"prompt": "Question?", "options": ["A", "B", "C", "D", "E"]},
        {"prompt": "Question?", "options": ["A", " A "]},
        {"prompt": "Question?", "options": ["A", " "]},
        {"prompt": "Question?", "response": "Model-invented answer"},
    ],
)
def test_question_schema_rejects_ambiguous_or_model_supplied_answers(arguments):
    with pytest.raises(ValidationError):
        questions.AskTeacherInput.model_validate(arguments)


def test_questions_are_root_only_and_require_the_pinned_capability():
    for run, code in (
        (SimpleNamespace(parent_run_id=None, manifest={}), "agent_question_unsupported"),
        (
            SimpleNamespace(
                parent_run_id=uuid4(),
                manifest={"interactive_version": questions.INTERACTIVE_VERSION},
            ),
            "agent_root_required",
        ),
    ):
        with pytest.raises(DomainError) as failure:
            questions.require_supported(run)
        assert failure.value.code == code


@pytest.fixture
async def question_run(client, db):
    workspace = await post(
        client,
        "/v1/workspaces",
        {
            "course_name": "Question course",
            "classroom_name": "Question classroom",
            "students": [{"external_id": "question-student", "name": "Student"}],
        },
    )
    created = await post(
        client,
        f"/v1/courses/{workspace['course_id']}/lesson-agent-runs",
        {
            "brief": {
                "topic": "补充教学要求",
                "objectives": ["区分提问与教师确认"],
                "duration_minutes": 10,
            }
        },
    )
    call = ToolCall.model_validate(
        {
            "id": "teacher-question-1",
            "function": {
                "name": "ask_teacher",
                "arguments": json.dumps(
                    {"prompt": "学生是否已经学习过分数？", "options": ["学过", "未学过"]},
                    ensure_ascii=False,
                ),
            },
        }
    )
    async with db.sessions.begin() as session:
        root, run = await scheduler.lock_run(session, UUID(created["id"]))
        run.manifest = {**run.manifest, "interactive_version": questions.INTERACTIVE_VERSION}
        run.profile_id = "agent-" + digest(run.manifest)
        run.status = "running"
        run.lease_owner = "question-test"
        run.lease_until = run.expires_at
        run.messages = [
            *run.messages,
            Reply(
                tool_calls=[call],
                metadata={
                    "requested_model": "fake",
                    "returned_model": "fake",
                    "adapter_version": "questions-fixture",
                    "usage": {},
                },
            ).message(),
        ]
        run.pending_calls = [call.model_dump(mode="json")]
        run.tools_used = root.tree_tools_used = 1
        run.models_used = root.tree_models_used = 1
    return UUID(created["id"]), call


async def ask(db, run_id, call):
    async with db.sessions.begin() as session:
        _, run = await scheduler.lock_run(session, run_id)
        request = await questions.ask_teacher(
            session, run, questions.AskTeacherInput.model_validate_json(call.function.arguments)
        )
        return await questions.begin_question(session, run, call, request)


async def answer(client, run_id, token, **fields):
    return await post(
        client,
        f"/v1/agent-runs/{run_id}/wake",
        {"wait_token": str(token), **fields},
    )


@pytest.mark.postgres
async def test_question_wait_is_durable_visible_and_does_not_auto_wake(db, client, question_run):
    run_id, call = question_run
    token = await ask(db, run_id, call)
    detail = (await client.get(f"/v1/agent-runs/{run_id}")).json()
    assert detail["status"] == "waiting_input"
    assert detail["wait"]["question"] == {
        "tool_call_id": call.id,
        "prompt": "学生是否已经学习过分数？",
        "options": ["学过", "未学过"],
    }
    history = (await client.get(f"/v1/agent-runs/{run_id}/waits")).json()["items"]
    assert history[0]["question"] == detail["wait"]["question"]
    assert history[0]["wait_token"] == str(token)
    assert await waiting.scan_due_waits(db) == 0
    async with db.sessions() as session:
        run = await session.get(AgentRun, run_id)
        assert run.pending_calls == [call.model_dump(mode="json")]
        assert run.lease_until is None and run.lease_owner is None
        assert run.models_used == run.tools_used == 1
        assert await questions.pending_answer(session, run) is None


@pytest.mark.postgres
async def test_question_requires_an_actual_teacher_answer(db, client, question_run):
    run_id, call = question_run
    token = await ask(db, run_id, call)
    path = f"/v1/agent-runs/{run_id}/wake"
    missing = await post(client, path, {"wait_token": str(token)}, expected=422)
    assert missing["code"] == "agent_answer_required"
    await post(client, path, {"wait_token": str(token), "response": " "}, expected=422)
    with pytest.raises(DomainError) as failure:
        async with db.sessions.begin() as session:
            _, run = await scheduler.lock_run(session, run_id)
            await waiting.wake_tree(session, run, token, response="学过", source="timer")
    assert failure.value.code == "agent_question_timer_forbidden"
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AgentWake)) == 0
        assert (await session.get(AgentRun, run_id)).status == "waiting_input"


@pytest.mark.postgres
async def test_answer_restarts_with_one_paired_receipt_and_no_extra_budget(
    db, client, question_run
):
    run_id, call = question_run
    token = await ask(db, run_id, call)
    responses = await asyncio.gather(
        answer(client, run_id, token, response="学过，请使用复习活动。"),
        answer(client, run_id, token, response="学过，请使用复习活动。"),
    )
    assert all(item["status"] == "queued" for item in responses)
    async with db.sessions.begin() as session:
        _, run = await scheduler.lock_run(session, run_id)
        received, output = await questions.pending_answer(session, run)
        assert received == call
        assert output == {
            "ok": True,
            "result": {
                "question": "学生是否已经学习过分数？",
                "response": "学过，请使用复习活动。",
                "wait_token": str(token),
            },
        }
        assert run.pending_user_messages == []
        assert run.models_used == run.tree_models_used == run.tools_used == run.tree_tools_used == 1
    # A new process/session sees the same immutable response until paired atomically.
    async with db.sessions.begin() as session:
        _, run = await scheduler.lock_run(session, run_id)
        assert (await questions.pending_answer(session, run))[1] == output
        run.messages = [*run.messages, tool_result(call.id, output)]
        run.pending_calls = run.pending_calls[1:]
    async with db.sessions() as session:
        run = await session.get(AgentRun, run_id)
        assert await questions.pending_answer(session, run) is None
        assert sum(item.get("tool_call_id") == call.id for item in run.messages) == 1
        assert await session.scalar(select(func.count()).select_from(AgentWake)) == 1
    different = await post(
        client,
        f"/v1/agent-runs/{run_id}/wake",
        {"wait_token": str(token), "response": "未学过"},
        expected=409,
    )
    assert different["code"] == "agent_wait_already_resolved"


@pytest.mark.postgres
async def test_another_teacher_cannot_read_or_answer_the_question(
    db, settings, client, question_run
):
    run_id, call = question_run
    token = await ask(db, run_id, call)
    other_settings = settings.model_copy(update={"dev_api_token": "other-" + uuid4().hex})
    await bootstrap(db, other_settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(other_settings, db)),
        base_url="http://test",
        headers={"Authorization": f"Bearer {other_settings.dev_api_token}"},
    ) as other:
        await post(
            other,
            f"/v1/agent-runs/{run_id}/wake",
            {"wait_token": str(token), "response": "越权回答"},
            expected=404,
        )
        assert (await other.get(f"/v1/agent-runs/{run_id}")).status_code == 404
        assert (await other.get(f"/v1/agent-runs/{run_id}/waits")).status_code == 404
    assert (await client.get(f"/v1/agent-runs/{run_id}")).json()["status"] == "waiting_input"


@pytest.mark.postgres
async def test_cancel_pairs_the_pending_question_and_rejects_late_answer(db, client, question_run):
    run_id, call = question_run
    token = await ask(db, run_id, call)
    await post(client, f"/v1/agent-runs/{run_id}/cancel", {})
    await post(
        client,
        f"/v1/agent-runs/{run_id}/wake",
        {"wait_token": str(token), "response": "迟到回答"},
        expected=409,
    )
    async with db.sessions() as session:
        run = await session.get(AgentRun, run_id)
        assert run.status == "cancelled" and run.pending_calls == []
        assert await questions.pending_answer(session, run) is None
        assert sum(item.get("tool_call_id") == call.id for item in run.messages) == 1
        receipt = await session.scalar(select(AgentWake).where(AgentWake.wait_id == token))
        assert receipt.source == "cancelled" and receipt.response is None


@pytest.mark.postgres
async def test_question_control_cannot_be_forged_or_detached_from_its_arguments(db, question_run):
    run_id, call = question_run
    for request in (
        {"prompt": "学生是否已经学习过分数？", "options": ["学过", "未学过"]},
        questions.TeacherInputRequest("另一个问题", ("学过", "未学过")),
    ):
        with pytest.raises(DomainError) as failure:
            async with db.sessions.begin() as session:
                _, run = await scheduler.lock_run(session, run_id)
                await questions.begin_question(session, run, call, request)
        assert failure.value.code == "invalid_teacher_question"
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AgentWait)) == 0


@pytest.mark.postgres
async def test_changed_pending_question_cannot_consume_the_saved_answer(db, client, question_run):
    run_id, call = question_run
    token = await ask(db, run_id, call)
    async with db.sessions.begin() as session:
        _, run = await scheduler.lock_run(session, run_id)
        altered = call.model_dump(mode="json")
        altered["function"]["arguments"] = '{"prompt":"Changed question?"}'
        run.pending_calls = [altered]
    response = await post(
        client,
        f"/v1/agent-runs/{run_id}/wake",
        {"wait_token": str(token), "response": "学过"},
        expected=422,
    )
    assert response["code"] == "agent_question_corrupt"
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AgentWake)) == 0


@pytest.mark.postgres
async def test_question_cannot_attach_a_timer(db, question_run):
    run_id, call = question_run
    with pytest.raises(DomainError) as failure:
        async with db.sessions.begin() as session:
            _, run = await scheduler.lock_run(session, run_id)
            now = await session.scalar(select(func.clock_timestamp()))
            await waiting.pause_tree(
                session,
                run,
                reason="Question",
                wake_at=now + timedelta(seconds=30),
                question={"tool_call_id": call.id, "prompt": "Question?", "options": None},
            )
    assert failure.value.code == "agent_question_timer_forbidden"


@pytest.mark.postgres
async def test_interrupted_question_transaction_can_retry_without_duplicate_wait(db, question_run):
    run_id, call = question_run
    with pytest.raises(RuntimeError, match="crash before commit"):
        async with db.sessions.begin() as session:
            _, run = await scheduler.lock_run(session, run_id)
            arguments = questions.AskTeacherInput.model_validate_json(call.function.arguments)
            request = await questions.ask_teacher(session, run, arguments)
            await questions.begin_question(session, run, call, request)
            raise RuntimeError("crash before commit")
    token = await ask(db, run_id, call)
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AgentWait)) == 1
        assert (await session.get(AgentRun, run_id)).wait_state["token"] == str(token)


@pytest.mark.postgres
async def test_assisted_worker_restarts_after_real_answer_without_charging_tool_twice(
    db, settings, client, question_run
):
    fixture_id, _ = question_run
    async with db.sessions() as session:
        fixed_input = (await session.get(AgentRun, fixture_id)).task_input
    started = await post(
        client,
        f"/v1/courses/{fixed_input['course_id']}/lesson-agent-runs",
        {"brief": fixed_input["brief"], "agent_profile": "fake-assisted-lesson-agent-v1"},
    )
    run_id = UUID(started["id"])
    assert await HarnessWorker(db, settings, run_ids=(run_id,)).run_once()
    paused = (await client.get(f"/v1/agent-runs/{run_id}")).json()
    assert paused["status"] == "waiting_input"
    assert paused["models_used"] == paused["tools_used"] == 3
    question = paused["wait"]["question"]
    response = "  先独立思考再交流，保留教师的原始要求。  "
    await answer(client, run_id, paused["wait"]["token"], response=response)
    assert await HarnessWorker(db, settings, run_ids=(run_id,)).run_once()
    finished = (await client.get(f"/v1/agent-runs/{run_id}")).json()
    assert finished["status"] == "waiting_teacher"
    assert finished["models_used"] == finished["tree_models_used"] == 4
    assert finished["tools_used"] == finished["tree_tools_used"] == 4
    assert finished["attempts"] == 1
    async with db.sessions() as session:
        run = await session.get(AgentRun, run_id)
        artifact = await session.scalar(select(AgentArtifact).where(AgentArtifact.run_id == run_id))
        assert artifact.body["teacher_response"] == response
        assert artifact.confirmed_at is None
        receipts = [
            item for item in run.messages if item.get("tool_call_id") == question["tool_call_id"]
        ]
        assert len(receipts) == 1
        assert json.loads(receipts[0]["content"])["result"]["response"] == response
        assert not any(
            item.get("role") == "user" and item.get("content") == response for item in run.messages
        )


@pytest.mark.postgres
async def test_unknown_question_on_old_task_is_a_tool_error_and_cannot_gain_waits(
    db, settings, question_run
):
    run_id, call = question_run
    async with db.sessions.begin() as session:
        _, run = await scheduler.lock_run(session, run_id)
        run.manifest = catalog.v3_manifest(settings, "lesson-plan", "fake-lesson-agent-v1")
        run.profile_id = "agent-" + digest(run.manifest)
        run.status = "queued"
        run.lease_owner = run.lease_until = None
        assert await questions.pending_answer(session, run) is None
    assert await HarnessWorker(db, settings, run_ids=(run_id,)).run_once()
    async with db.sessions() as session:
        run = await session.get(AgentRun, run_id)
        assert run.status == "waiting_teacher"
        receipt = next(item for item in run.messages if item.get("tool_call_id") == call.id)
        assert json.loads(receipt["content"])["error_code"] == "tool_not_allowed"
        assert await session.scalar(select(func.count()).select_from(AgentWait)) == 0


@pytest.mark.postgres
async def test_existing_question_cannot_be_consumed_after_capability_changes(
    db, client, question_run
):
    run_id, call = question_run
    token = await ask(db, run_id, call)
    await answer(client, run_id, token, response="学过")
    with pytest.raises(DomainError) as failure:
        async with db.sessions.begin() as session:
            _, run = await scheduler.lock_run(session, run_id)
            run.manifest = {
                key: value for key, value in run.manifest.items() if key != "interactive_version"
            }
            await questions.pending_answer(session, run)
    assert failure.value.code == "agent_question_unsupported"

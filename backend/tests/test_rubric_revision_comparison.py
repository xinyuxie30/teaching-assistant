from copy import deepcopy
from uuid import UUID, uuid4

import httpx
import pytest
from conftest import post
from sqlalchemy import select
from teaching_assistant.agent.worker import AgentWorker
from teaching_assistant.api import create_app
from teaching_assistant.cli import bootstrap
from teaching_assistant.db import AgentRun, RubricDraft
from teaching_assistant.domain import digest

pytestmark = pytest.mark.postgres


async def revise(client, db, settings, scenario):
    run = await post(
        client,
        f"/v1/question-versions/{scenario['question']['id']}/rubric-agent-runs",
        {
            "source_draft_id": scenario["draft"]["id"],
            "instructions": "细化等级描述。",
        },
    )
    await AgentWorker(db, settings, run_ids=(UUID(run["id"]),)).run_once()
    run = (await client.get(f"/v1/agent-runs/{run['id']}")).json()
    return run, (await client.get(f"/v1/rubric-drafts/{run['draft_id']}")).json()


async def test_draft_detail_compares_the_captured_source_and_preserves_it_after_confirmation(
    db, settings, client, scenario
):
    run, detail = await revise(client, db, settings, scenario)
    comparison = detail["revision_comparison"]
    assert comparison["run_id"] == run["id"]
    assert comparison["source"]["id"] == scenario["draft"]["id"]
    assert comparison["source"]["revision"] == 1
    assert comparison["source"]["content_hash"] == digest(comparison["source_body"])
    assert comparison["feedback"] == "细化等级描述。"
    assert comparison["target"] == {
        "id": detail["id"],
        "revision": detail["revision"],
        "content_hash": digest(detail["body"]),
    }
    # The simulator produces one new point instead of guessing a match to either old ID.
    assert comparison["difference"]["summary"] == {
        "added": 1,
        "removed": 2,
        "modified": 0,
        "unchanged": 0,
    }
    async with db.sessions.begin() as session:
        source = await session.get(RubricDraft, UUID(scenario["draft"]["id"]))
        source.body = {**source.body, "global_instructions": "后续编辑内容"}
        source.revision += 1
    again = (await client.get(f"/v1/rubric-drafts/{detail['id']}")).json()
    assert again == detail
    await post(client, f"/v1/rubric-drafts/{detail['id']}/confirm", {"expected_revision": 1})
    confirmed = (await client.get(f"/v1/rubric-drafts/{detail['id']}")).json()
    assert confirmed["confirmed_version_id"]
    assert confirmed["revision_comparison"] == comparison
    async with db.sessions() as session:
        stored = await session.get(AgentRun, UUID(run["id"]))
        assert stored.models_used == 2 and stored.tools_used == 2


async def test_ordinary_drafts_have_no_implicit_comparison_and_exact_revisions_report_no_change(
    db, settings, client, scenario
):
    ordinary = (await client.get(f"/v1/rubric-drafts/{scenario['draft']['id']}")).json()
    assert ordinary["revision_comparison"] is None
    path = f"/v1/question-versions/{scenario['question']['id']}/rubric-agent-runs"
    parent = await post(client, path, {})
    await AgentWorker(db, settings, run_ids=(UUID(parent["id"]),)).run_once()
    parent = (await client.get(f"/v1/agent-runs/{parent['id']}")).json()
    source = (await client.get(f"/v1/rubric-drafts/{parent['draft_id']}")).json()
    assert source["revision_comparison"] is None
    child = await post(
        client, path, {"source_draft_id": source["id"], "instructions": "保持当前描述"}
    )
    await AgentWorker(db, settings, run_ids=(UUID(child["id"]),)).run_once()
    child = (await client.get(f"/v1/agent-runs/{child['id']}")).json()
    detail = (await client.get(f"/v1/rubric-drafts/{child['draft_id']}")).json()
    assert not detail["revision_comparison"]["difference"]["has_changes"]
    assert detail["revision_comparison"]["difference"]["summary"]["unchanged"] == 1


async def test_comparison_cannot_leak_a_draft_or_its_source_to_another_teacher(
    db, settings, client, scenario
):
    run, detail = await revise(client, db, settings, scenario)
    other_settings = settings.model_copy(update={"dev_api_token": "other-" + uuid4().hex})
    await bootstrap(db, other_settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(other_settings, db)),
        base_url="http://test",
        headers={"Authorization": f"Bearer {other_settings.dev_api_token}"},
    ) as other:
        for draft_id in (detail["id"], scenario["draft"]["id"], str(uuid4())):
            response = await other.get(f"/v1/rubric-drafts/{draft_id}")
            assert response.status_code == 404
            assert "source_body" not in response.text and "细化等级描述" not in response.text
    # Reads cannot mutate the recorded source or create additional model work.
    async with db.sessions() as session:
        before = deepcopy((await session.get(AgentRun, UUID(run["id"]))).revision_context)
    await client.get(f"/v1/rubric-drafts/{detail['id']}")
    async with db.sessions() as session:
        stored = await session.scalar(select(AgentRun).where(AgentRun.id == UUID(run["id"])))
        assert stored.revision_context == before and stored.models_used == 2

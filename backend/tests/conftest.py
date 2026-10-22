import os
from uuid import uuid4

import httpx
import pytest
from alembic import command
from alembic.config import Config
from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from teaching_assistant.api import create_app
from teaching_assistant.cli import bootstrap
from teaching_assistant.db import Database
from teaching_assistant.settings import Settings

load_dotenv()


@pytest.fixture
async def db():
    url = os.environ.get("TA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("TA_TEST_DATABASE_URL is required for real PostgreSQL tests")
    if not make_url(url).database.endswith("_test"):
        raise RuntimeError("Integration tests require a separate database ending in _test")
    # Every test uses its own random schema. Never truncate the application's tables.
    schema = f"test_{uuid4().hex}"
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    database = Database(url, schema=schema)
    try:
        async with database.engine.begin() as connection:

            def upgrade(sync_connection):
                config = Config("alembic.ini")
                config.attributes["connection"] = sync_connection
                command.upgrade(config, "head")

            await connection.run_sync(upgrade)
        yield database
    finally:
        await database.close()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


@pytest.fixture
def settings():
    return Settings(
        dev_api_token=f"test-token-{uuid4().hex}",
        jev_model=None,
        jev_api_key=None,
        jev_proxy=None,
        deepseek_model=None,
        deepseek_api_key=None,
        deepseek_proxy=None,
        call_timeout_seconds=0.3,
        lease_seconds=5,
        retry_delay_seconds=0,
    )


@pytest.fixture
async def client(db, settings):
    await bootstrap(db, settings)
    app = create_app(settings, db)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {settings.dev_api_token}"},
    ) as http_client:
        yield http_client


async def post(client, path, payload, *, key=None, expected=None):
    response = await client.post(
        path, json=payload, headers={"Idempotency-Key": key or uuid4().hex}
    )
    if expected is None:
        assert response.status_code < 300, response.text
    else:
        assert response.status_code == expected, response.text
    return response.json()


@pytest.fixture
async def scenario(client):
    from teaching_assistant.demo import DEMO_RUBRIC

    workspace = await post(
        client,
        "/v1/workspaces",
        {
            "course_name": "Test course",
            "classroom_name": "Test classroom",
            "students": [
                {"external_id": "s1", "name": "=unsafe-csv-name"},
                {"external_id": "s2", "name": "Student 2"},
            ],
        },
    )
    assignment = await post(
        client,
        f"/v1/courses/{workspace['course_id']}/assignments",
        {
            "classroom_id": workspace["classroom_id"],
            "title": "Test assignment",
        },
    )
    question = await post(
        client,
        f"/v1/assignments/{assignment['id']}/question-versions",
        {
            "question_key": "q1",
            "prompt": "Explain versioning.",
            "reference": "Consistency and history.",
            "maximum_points": "10.00",
        },
    )
    draft = await post(
        client,
        f"/v1/question-versions/{question['id']}/rubric-drafts",
        {
            "rubric": DEMO_RUBRIC,
        },
    )
    rubric = await post(
        client, f"/v1/rubric-drafts/{draft['id']}/confirm", {"expected_revision": 1}
    )
    rows = [
        {
            "student_id": student["id"],
            "question_version_id": question["id"],
            "external_submission_id": f"submission-{i}",
            "source_revision": "initial",
            "answer_text": text,
        }
        for i, (student, text) in enumerate(
            zip(
                workspace["students"],
                [
                    "  😀统一规则。\r\n历史可以复核。  ",
                    "不确定。",
                ],
                strict=True,
            )
        )
    ]
    imported = await post(client, f"/v1/assignments/{assignment['id']}/imports", {"rows": rows})
    batch_payload = {
        "rubric_version_id": rubric["id"],
        "answer_version_ids": imported["answer_version_ids"],
    }
    batch = await post(client, "/v1/grading-batches", batch_payload)
    return {
        "workspace": workspace,
        "assignment": assignment,
        "question": question,
        "draft": draft,
        "rubric": rubric,
        "rows": rows,
        "imported": imported,
        "batch": batch,
        "batch_payload": batch_payload,
    }

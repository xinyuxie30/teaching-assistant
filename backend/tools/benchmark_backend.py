"""Synthetic database/API benchmark; no model calls and no business database writes."""

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import httpx
from alembic import command
from alembic.config import Config
from dotenv import load_dotenv
from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from teaching_assistant.api import create_app
from teaching_assistant.cli import bootstrap
from teaching_assistant.db import Database
from teaching_assistant.demo import DEMO_RUBRIC
from teaching_assistant.settings import Settings

REPO = Path(__file__).resolve().parents[2]


async def post(client, path, payload):
    response = await client.post(path, json=payload, headers={"Idempotency-Key": uuid4().hex})
    response.raise_for_status()
    return response.json()


async def measure(db, client, path, payload):
    statements = 0

    def count(*_):
        nonlocal statements
        statements += 1

    event.listen(db.engine.sync_engine, "before_cursor_execute", count)
    started = perf_counter()
    try:
        response = await post(client, path, payload)
    finally:
        elapsed = perf_counter() - started
        event.remove(db.engine.sync_engine, "before_cursor_execute", count)
    return response, {"sql_statements": statements, "elapsed_ms": round(elapsed * 1000, 2)}


async def scenario(db, client, size):
    workspace = await post(
        client,
        "/v1/workspaces",
        {
            "course_name": "Synthetic backend benchmark",
            "classroom_name": f"Synthetic class of {size}",
            "students": [
                {"external_id": f"fixture-{index}", "name": f"Synthetic {index}"}
                for index in range(size)
            ],
        },
    )
    assignment = await post(
        client,
        f"/v1/courses/{workspace['course_id']}/assignments",
        {"classroom_id": workspace["classroom_id"], "title": "Synthetic assignment"},
    )
    question = await post(
        client,
        f"/v1/assignments/{assignment['id']}/question-versions",
        {
            "question_key": "q1",
            "prompt": "Explain versioning.",
            "reference": "History.",
            "maximum_points": "10.00",
        },
    )
    draft = await post(
        client, f"/v1/question-versions/{question['id']}/rubric-drafts", {"rubric": DEMO_RUBRIC}
    )
    rubric = await post(
        client, f"/v1/rubric-drafts/{draft['id']}/confirm", {"expected_revision": 1}
    )
    payload = {
        "rows": [
            {
                "student_id": student["id"],
                "question_version_id": question["id"],
                "external_submission_id": f"fixture-{index}",
                "source_revision": "v1",
                "answer_text": "  Synthetic answer: preserve versions and history.\r\n",
            }
            for index, student in enumerate(workspace["students"])
        ]
    }
    path = f"/v1/assignments/{assignment['id']}/imports"
    _, preview = await measure(db, client, path + "/preview", payload)
    imported, commit = await measure(db, client, path, payload)
    batch_payload = {
        "rubric_version_id": rubric["id"],
        "answer_version_ids": imported["answer_version_ids"],
    }
    batch, create = await measure(db, client, "/v1/grading-batches", batch_payload)
    _, reuse = await measure(db, client, "/v1/grading-batches", batch_payload)
    exported, export = await measure(
        db, client, f"/v1/grading-batches/{batch['id']}/exports", {"mode": "working"}
    )
    assert len(exported["snapshot"]["rows"]) == size
    assert exported["snapshot"]["counts"]["confirmed"] == 0
    return {
        "students": size,
        "preview": preview,
        "import": commit,
        "create_batch": create,
        "reuse_batch": reuse,
        "export_pending": export,
    }


async def benchmark(output):
    load_dotenv(REPO / ".env")
    url = os.environ.get("TA_TEST_DATABASE_URL")
    if not url or not (make_url(url).database or "").endswith("_test"):
        raise ValueError("A separate *_test database is required")
    schema = "backend_benchmark_" + uuid4().hex
    admin = create_async_engine(url)
    db = Database(url, schema=schema)
    settings = Settings(
        _env_file=None,
        database_url=url,
        dev_api_token="synthetic-" + uuid4().hex,
        deepseek_model=None,
        deepseek_api_key=None,
        jev_model=None,
        jev_api_key=None,
    )
    report = {
        "synthetic": True,
        "model_calls": 0,
        "started_at": datetime.now(UTC).isoformat(),
        "cases": [],
    }
    created = False
    try:
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            created = True
        async with db.engine.begin() as connection:

            def migrate(sync_connection):
                config = Config(str(REPO / "alembic.ini"))
                config.attributes["connection"] = sync_connection
                command.upgrade(config, "head")

            await connection.run_sync(migrate)
        await bootstrap(db, settings)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(settings, db)),
            base_url="http://benchmark",
            headers={"Authorization": f"Bearer {settings.dev_api_token}"},
        ) as client:
            for size in (20, 200):
                result = await scenario(db, client, size)
                report["cases"].append(result)
                print(json.dumps(result), flush=True)
    finally:
        await db.close()
        if created:
            async with admin.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()
    report["schema_cleaned"] = True
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    if arguments.output.exists():
        parser.error("Output already exists; choose a new report path")
    asyncio.run(benchmark(arguments.output))

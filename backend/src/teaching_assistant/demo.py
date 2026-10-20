import asyncio
import json
import time
from pathlib import Path
from uuid import UUID

import httpx

from teaching_assistant.api import create_app
from teaching_assistant.db import Database
from teaching_assistant.settings import Settings
from teaching_assistant.worker import Worker

DEMO_RUBRIC = {
    "global_instructions": "仅按当前答案分别评估两个评分点。此题仅为合成工程演示。",
    "criteria": [
        {
            "id": "fixed_rule",
            "semantic_version": 1,
            "description": "说明固定评分标准使同次答案按同一规则评估",
            "maximum_points": "6.00",
            "levels": [
                {"id": "missing", "ordinal": 0, "description": "未提到统一规则", "points": "0.00"},
                {
                    "id": "partial",
                    "ordinal": 1,
                    "description": "提到固定但未说明统一评估",
                    "points": "3.00",
                },
                {
                    "id": "complete",
                    "ordinal": 2,
                    "description": "明确说明按统一固定规则评估",
                    "points": "6.00",
                },
            ],
        },
        {
            "id": "traceability",
            "semantic_version": 1,
            "description": "说明保留版本可以追溯与复核历史结果",
            "maximum_points": "4.00",
            "levels": [
                {"id": "missing", "ordinal": 0, "description": "未提到历史追溯", "points": "0.00"},
                {
                    "id": "complete",
                    "ordinal": 1,
                    "description": "明确说明追溯并复核旧结果",
                    "points": "4.00",
                },
            ],
        },
    ],
}


async def demo(db: Database, settings: Settings, run_id: str, output: str) -> dict:
    app = create_app(settings, db)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://demo",
        headers={"Authorization": f"Bearer {settings.dev_api_token}"},
    ) as client:

        async def post(path: str, body: dict, operation: str):
            response = await client.post(
                path, json=body, headers={"Idempotency-Key": f"{run_id}:{operation}"}
            )
            if response.is_error:
                raise RuntimeError(f"{path}: {response.status_code} {response.text}")
            return response.json()

        workspace = await post(
            "/v1/workspaces",
            {
                "course_name": "合成演示课程",
                "classroom_name": "演示班",
                "students": [
                    {"external_id": "DEMO-001", "name": "演示学生甲"},
                    {"external_id": "DEMO-002", "name": "演示学生乙"},
                ],
            },
            "workspace",
        )
        assignment = await post(
            f"/v1/courses/{workspace['course_id']}/assignments",
            {
                "classroom_id": workspace["classroom_id"],
                "title": "评分规则版本演示",
            },
            "assignment",
        )
        question = await post(
            f"/v1/assignments/{assignment['id']}/question-versions",
            {
                "question_key": "q1",
                "prompt": "说明锁定评分细则版本的两个作用。",
                "reference": "使用统一规则评估；保留历史标准以追溯复核。",
                "maximum_points": "10.00",
            },
            "question",
        )
        draft = await post(
            f"/v1/question-versions/{question['id']}/rubric-drafts",
            {
                "rubric": DEMO_RUBRIC,
            },
            "draft",
        )
        rubric = await post(
            f"/v1/rubric-drafts/{draft['id']}/confirm",
            {
                "expected_revision": 1,
            },
            "confirm",
        )
        imported = await post(
            f"/v1/assignments/{assignment['id']}/imports",
            {
                "rows": [
                    {
                        "student_id": student["id"],
                        "question_version_id": question["id"],
                        "external_submission_id": f"submission-{index}",
                        "source_revision": "initial",
                        "answer_text": text,
                    }
                    for index, (student, text) in enumerate(
                        zip(
                            workspace["students"],
                            [
                                "让所有答案按同一套固定规则评分，也可以追溯旧版本并复核结果。",
                                "方便老师管理。",
                            ],
                            strict=True,
                        )
                    )
                ],
            },
            "import",
        )
        batch = await post(
            "/v1/grading-batches",
            {
                "rubric_version_id": rubric["id"],
                "answer_version_ids": imported["answer_version_ids"],
            },
            "batch",
        )
        worker = Worker(db, settings, item_ids=tuple(UUID(i) for i in batch["item_ids"]))
        # Scope the demo worker to its selected items; do not drain unrelated teachers' work.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            state = (await client.get(f"/v1/grading-batches/{batch['id']}")).json()
            if state["status"] == "settled":
                if state["outcome"] != "all_succeeded":
                    raise RuntimeError("Demo grading failed; inspect the batch before retrying")
                break
            if not await worker.run_once():
                await asyncio.sleep(0.1)
        else:
            raise RuntimeError("Demo timed out waiting for grading; the batch remains recoverable")
        views = [
            (await client.get(f"/v1/grading-items/{item_id}")).json()
            for item_id in batch["item_ids"]
        ]
        first = next(v for v in views if v["answer"]["id"] == imported["answer_version_ids"][0])
        await post(
            f"/v1/grading-items/{first['id']}/reviews",
            {
                "expected_revision": 0,
                "decisions": [
                    {
                        "criterion_id": c["id"],
                        "level_id": "complete",
                        "evidence_support": "supported",
                        "reason": "合成示例：人工确认原答案明确覆盖此点。",
                    }
                    for c in DEMO_RUBRIC["criteria"]
                ],
            },
            "review",
        )
        exported = await post(
            f"/v1/grading-batches/{batch['id']}/exports",
            {
                "mode": "working",
            },
            "export",
        )

        def save_output() -> str:
            target = Path(output)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(exported, ensure_ascii=False, indent=2), encoding="utf-8")
            return str(target.resolve())

        target_path = await asyncio.to_thread(save_output)
        return {
            "notice": "fake-v1 固定选择最低等级，仅证明工程链路。演示复核为脚本模拟老师确认。",
            "batch_id": batch["id"],
            "export_id": exported["id"],
            "counts": exported["snapshot"]["counts"],
            "output": target_path,
        }

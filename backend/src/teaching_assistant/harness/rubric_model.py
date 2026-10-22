"""Deterministic rubric model used only for offline engineering verification."""

import json

from teaching_assistant.domain import digest
from teaching_assistant.harness.contracts import Reply


class FakeAgentModel:
    async def respond(self, body, policy):
        question = next(
            (
                output["result"]
                for m in body["messages"]
                if m["role"] == "tool"
                for output in [json.loads(m["content"])]
                if output.get("ok") and "maximum_points" in output.get("result", {})
            ),
            None,
        )
        if question is None:
            name, arguments = "read_question", {}
        else:
            name = "propose_rubric"
            arguments = {
                "rubric": {
                    "global_instructions": "模拟草稿，仅用于工程验证，老师必须检查并确认。",
                    "criteria": [
                        {
                            "id": "demo",
                            "description": "模拟评分点（不代表教学标准）",
                            "maximum_points": question["maximum_points"],
                            "levels": [
                                {
                                    "id": "missing",
                                    "ordinal": 0,
                                    "description": "模拟未体现",
                                    "points": "0.00",
                                },
                                {
                                    "id": "complete",
                                    "ordinal": 1,
                                    "description": "模拟完整体现",
                                    "points": question["maximum_points"],
                                },
                            ],
                        }
                    ],
                }
            }
        return Reply.model_validate(
            {
                "tool_calls": [
                    {
                        "id": "fake-" + digest(body)[:32],
                        "function": {
                            "name": name,
                            "arguments": json.dumps(arguments, ensure_ascii=False),
                        },
                    }
                ],
                "metadata": {
                    "requested_model": policy["model"],
                    "returned_model": policy["model"],
                    "adapter_version": "fake-rubric-agent-v1",
                    "usage": {},
                    "prompt_hash": digest(body),
                },
            }
        )

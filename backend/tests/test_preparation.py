"""Assignment preparation must use the frozen roster, even after class membership changes."""

from uuid import UUID

import pytest
from sqlalchemy import delete
from teaching_assistant.db import Membership, Student
from test_listings import listing

pytestmark = pytest.mark.postgres


async def test_assignment_detail_and_import_keep_the_original_roster(client, db, scenario):
    workspace = scenario["workspace"]
    path = f"/v1/assignments/{scenario['assignment']['id']}"
    before = await listing(client, path)
    assert before["course_name"] == "Test course"
    assert before["classroom_name"] == "Test classroom"
    assert before["course_id"] == workspace["course_id"]
    assert before["classroom_id"] == workspace["classroom_id"]
    assert before["title"] == "Test assignment" and before["created_at"]
    assert before["roster"] == workspace["students"]

    async with db.sessions.begin() as session:
        await session.execute(
            delete(Membership).where(
                Membership.classroom_id == UUID(workspace["classroom_id"]),
                Membership.student_id == UUID(workspace["students"][0]["id"]),
            )
        )
        newcomer = Student(
            course_id=UUID(workspace["course_id"]), external_id="s3", name="New class member"
        )
        session.add(newcomer)
        await session.flush()
        newcomer_id = str(newcomer.id)
        session.add(
            Membership(classroom_id=UUID(workspace["classroom_id"]), student_id=newcomer.id)
        )

    after = await listing(client, path)
    assert after == before
    current = await listing(
        client,
        f"/v1/courses/{workspace['course_id']}/students",
        classroom_id=workspace["classroom_id"],
    )
    assert {student["id"] for student in current["items"]} == {
        newcomer_id,
        workspace["students"][1]["id"],
    }

    # An original student is still valid for this assignment after leaving the class.
    original = dict(scenario["rows"][0])
    response = await client.post(f"{path}/imports/preview", json={"rows": [original]})
    assert response.status_code == 200 and response.json()["reused"] == 1
    # A newly enrolled student has no retroactive permission to join the old assignment.
    response = await client.post(
        f"{path}/imports/preview", json={"rows": [{**original, "student_id": newcomer_id}]}
    )
    assert response.status_code == 422 and response.json()["code"] == "invalid_import_scope"

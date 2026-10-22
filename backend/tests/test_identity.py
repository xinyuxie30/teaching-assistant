import httpx
import pytest
from sqlalchemy import select
from teaching_assistant.api import create_app
from teaching_assistant.cli import bootstrap, rotate_token
from teaching_assistant.db import Audit
from teaching_assistant.settings import Settings


async def test_rotating_the_development_token_keeps_the_same_teacher(db, settings):
    issued = await bootstrap(db, settings)
    new_token = "rotated-" + "x" * 24
    rotated = await rotate_token(db, Settings(dev_api_token=new_token), "开发老师")
    assert rotated == {
        "teacher_id": issued["teacher_id"],
        "rotated": True,
        "identity_mode": "local-development-token",
    }

    app = create_app(Settings(dev_api_token=new_token), db)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        current = await client.get("/v1/me", headers={"Authorization": f"Bearer {new_token}"})
        assert current.status_code == 200
        assert current.json()["teacher_id"] == issued["teacher_id"]
        old = await client.get(
            "/v1/me", headers={"Authorization": f"Bearer {settings.dev_api_token}"}
        )
        assert old.status_code == 401

    async with db.sessions() as session:
        actions = list(
            await session.scalars(
                select(Audit.action).where(Audit.action == "developer_token_rotated")
            )
        )
    assert actions == ["developer_token_rotated"]


async def test_rotation_refuses_ambiguous_or_taken_tokens(db, settings):
    await bootstrap(db, settings)
    with pytest.raises(ValueError, match="exactly one teacher"):
        await rotate_token(db, Settings(dev_api_token="missing-" + "z" * 24), "不存在的老师")

    other = Settings(dev_api_token="taken-" + "y" * 24)
    await bootstrap(db, other, "另一位老师")
    with pytest.raises(ValueError, match="another teacher"):
        await rotate_token(db, other, "开发老师")

    unchanged = await rotate_token(db, settings, "开发老师")
    assert unchanged["rotated"] is False
    assert unchanged["reason"] == "already_current"


async def test_rotation_requires_a_real_token(db, settings):
    await bootstrap(db, settings)
    with pytest.raises(ValueError, match="at least 20 characters"):
        await rotate_token(db, Settings(dev_api_token="short"), "开发老师")

"""The destructive drill must stay isolated and actually interrupt its sockets."""

import asyncio
import importlib.util
from pathlib import Path

import pytest
from teaching_assistant.worker_backoff import failure_delay

_SPEC = importlib.util.spec_from_file_location(
    "runtime_stress_support", Path(__file__).parents[1] / "tools" / "runtime_stress_support.py"
)
support = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(support)


@pytest.mark.parametrize(
    "url",
    [
        "sqlite:///fixture_test",
        "postgresql+asyncpg://localhost/teaching",
        "postgresql+asyncpg://localhost/teaching_test_copy",
        "postgresql+asyncpg://localhost",
    ],
)
def test_drill_rejects_non_test_database(url):
    with pytest.raises(ValueError, match="separate PostgreSQL"):
        support.test_url(url)


@pytest.mark.parametrize("schema", ["public", "runtime_stress_a", 'runtime_stress_"; DROP'])
def test_drill_rejects_non_owned_schema(schema):
    with pytest.raises(ValueError, match="drill-owned random schema"):
        support.test_schema(schema)


def test_drill_disables_inherited_paid_provider_configuration(monkeypatch):
    for key in ("DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "JEV_API_KEY", "JEV_MODEL"):
        monkeypatch.setenv("TA_" + key, "synthetic-inherited-value")
    settings = support.settings_for("postgresql+asyncpg://localhost/fixture_test")
    assert settings.deepseek_api_key is settings.deepseek_model is None
    assert settings.jev_api_key is settings.jev_model is None


def test_failure_backoff_is_bounded_and_preserves_slower_configured_polling():
    assert failure_delay(0.1, 100000) == 2.0
    assert failure_delay(10, 100000) == 10


async def test_proxy_cuts_existing_connections_rejects_new_ones_and_recovers():
    handlers = set()

    async def echo(reader, writer):
        handlers.add(asyncio.current_task())
        try:
            while data := await reader.read(1024):
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            handlers.discard(asyncio.current_task())

    server = await asyncio.start_server(echo, "127.0.0.1", 0)
    proxy = support.DatabaseProxy("127.0.0.1", server.sockets[0].getsockname()[1])
    port = await proxy.start()
    writers = []
    try:
        async with asyncio.timeout(5):
            first, writer = await asyncio.open_connection("127.0.0.1", port)
            writers.append(writer)
            writer.write(b"fixture")
            await writer.drain()
            assert await first.readexactly(7) == b"fixture"
            assert proxy.disconnect() == 1
            assert await first.read(1) == b""
            rejected, writer = await asyncio.open_connection("127.0.0.1", port)
            writers.append(writer)
            assert await rejected.read(1) == b""
            assert proxy.rejected == 1
            proxy.restore()
            recovered, writer = await asyncio.open_connection("127.0.0.1", port)
            writers.append(writer)
            writer.write(b"recovered")
            await writer.drain()
            assert await recovered.readexactly(9) == b"recovered"
            assert proxy.accepted == 2
    finally:
        for writer in writers:
            writer.close()
        await proxy.close()
        server.close()
        await server.wait_closed()
        await asyncio.gather(*handlers)
    assert not proxy.tasks and not proxy.writers

"""Explicit browser check: pytest backend/tests/class_practice_browser_check.py.

Uses the existing isolated-db fixture; starts only a loopback test API and Vite.
"""

import asyncio
import contextlib
import os
import socket
from pathlib import Path

import httpx
import uvicorn
from teaching_assistant.api import create_app
from teaching_assistant.harness.worker import HarnessWorker
from test_class_practice import prepared as prepared_fixture

prepared = prepared_fixture
ROOT = Path(__file__).resolve().parents[2]


def free_socket():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    return sock


async def test_class_practice_browser(db, settings, client, prepared):
    root = ROOT
    api_socket = free_socket()
    api_port = api_socket.getsockname()[1]
    with free_socket() as temporary:
        frontend_port = temporary.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(settings, db), log_level="error"))
    serving = asyncio.create_task(server.serve(sockets=[api_socket]))
    env = {
        **os.environ,
        "TA_API_ORIGIN": f"http://127.0.0.1:{api_port}",
        "TA_E2E_BASE_URL": f"http://127.0.0.1:{frontend_port}",
        "TA_E2E_TOKEN": settings.dev_api_token,
    }
    vite = await asyncio.create_subprocess_exec(
        "node",
        "node_modules/vite/bin/vite.js",
        "--port",
        str(frontend_port),
        cwd=root / "frontend",
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )

    async def worker():
        engine = HarnessWorker(db, settings)
        while True:
            await engine.run_once()
            await asyncio.sleep(0.2)

    working = asyncio.create_task(worker())
    browser = None
    try:
        async with asyncio.timeout(30), httpx.AsyncClient(trust_env=False) as http:
            while True:
                try:
                    if (
                        await http.get(env["TA_E2E_BASE_URL"])
                    ).status_code == 200 and server.started:
                        break
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.2)
        browser = await asyncio.create_subprocess_exec(
            "node",
            "--test",
            "practice.test.mjs",
            cwd=root / "frontend/e2e",
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        async with asyncio.timeout(180):
            output, _ = await browser.communicate()
        assert browser.returncode == 0, output.decode("utf-8", errors="replace").replace(
            settings.dev_api_token, "[REDACTED]"
        )
    finally:
        if browser is not None and browser.returncode is None:
            browser.terminate()
            await browser.wait()
        working.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await working
        if vite.returncode is None:
            vite.terminate()
            await vite.wait()
        server.should_exit = True
        await serving
        api_socket.close()

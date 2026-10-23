"""Bounded, test-only network and process helpers for the runtime recovery drill."""

import asyncio
import os
import re
import subprocess
import sys
from pathlib import Path
from time import monotonic

from sqlalchemy.engine import make_url
from teaching_assistant.settings import Settings

REPO = Path(__file__).resolve().parents[2]


def test_url(value):
    url = make_url(value)
    if url.drivername != "postgresql+asyncpg" or not (url.database or "").endswith("_test"):
        raise ValueError("Recovery drills require a separate PostgreSQL *_test database")
    return url


def test_schema(value):
    if not re.fullmatch(r"runtime_stress_[0-9a-f]{32}", value):
        raise ValueError("Only a drill-owned random schema is allowed")
    return value


def settings_for(url):
    test_url(url)
    return Settings(
        _env_file=None,
        database_url=url,
        dev_api_token="synthetic-runtime-drill-token",
        jev_api_key=None,
        jev_model=None,
        deepseek_api_key=None,
        deepseek_model=None,
        call_timeout_seconds=2,
        lease_seconds=5,
        poll_seconds=0.1,
        retry_delay_seconds=0,
    )


async def until(predicate, *, deadline_seconds=30, label="condition"):
    try:
        async with asyncio.timeout(deadline_seconds):
            while True:
                result = await predicate()
                if result:
                    return result
                await asyncio.sleep(0.1)
    except TimeoutError:
        raise RuntimeError(f"Timed out waiting for {label}") from None


class DatabaseProxy:
    """Forward bytes without inspecting/logging them; cut only drill-worker sockets."""

    def __init__(self, host, port):
        self.host, self.port = host, port
        self.server = None
        self.writers, self.tasks = set(), set()
        self.blocked = False
        self.accepted = self.rejected = self.cut_connections = 0

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def forward(self, reader, writer):
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        peer = None
        pumps = []
        self.writers.add(writer)
        try:
            if self.blocked:
                self.rejected += 1
                return
            backend, peer = await asyncio.open_connection(self.host, self.port)
            self.writers.add(peer)
            if self.blocked:
                self.rejected += 1
                return
            self.accepted += 1
            pumps = [
                asyncio.create_task(self.forward(reader, peer)),
                asyncio.create_task(self.forward(backend, writer)),
            ]
            await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        except (ConnectionError, OSError):
            pass
        finally:
            for pump in pumps:
                pump.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
            for stream in (writer, peer):
                if stream is not None:
                    stream.close()
                    self.writers.discard(stream)
                    try:
                        await stream.wait_closed()
                    except (ConnectionError, OSError):
                        pass
            self.tasks.discard(task)

    def disconnect(self):
        self.blocked = True
        count = len(self.writers) // 2
        self.cut_connections += count
        for writer in tuple(self.writers):
            writer.close()
        return count

    def restore(self):
        self.blocked = False

    async def close(self):
        self.disconnect()
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        for task in tuple(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


class WorkerProcess:
    def __init__(self, process, log, stop, name, kind, signal):
        self.process, self.log, self.stop_file = process, log, stop
        self.name, self.kind, self.signal = name, kind, signal
        self.started = monotonic()

    @classmethod
    async def start(cls, directory, url, schema, name, kind, *, slots=1, gate=None):
        test_url(url)
        test_schema(schema)
        log = (directory / f"{name}.log").open("x", encoding="utf-8")
        stop = directory / f"{name}.stop"
        signal = directory / f"{name}.signal.json"
        args = [
            sys.executable,
            str(REPO / "backend/tools/runtime_stress_worker.py"),
            "--schema",
            schema,
            "--name",
            name,
            "--kind",
            kind,
            "--slots",
            str(slots),
            "--stop",
            str(stop),
            "--signal",
            str(signal),
        ]
        if gate is not None:
            args.extend(("--gate", gate))
        env = {
            **os.environ,
            "TA_STRESS_DATABASE_URL": url,
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8",
        }
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                cwd=REPO,
                env=env,
                stdout=log,
                stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except BaseException:
            log.close()
            raise
        return cls(process, log, stop, name, kind, signal)

    async def kill(self):
        if self.process.returncode is not None:
            raise RuntimeError(f"Worker {self.name} exited before fault injection")
        self.process.kill()  # The exact child handle created by this drill, never a PID scan.
        await asyncio.wait_for(self.process.wait(), timeout=10)
        self.log.close()
        return self.process.returncode

    async def stop(self):
        if self.process.returncode is None:
            self.stop_file.touch()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=10)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        self.log.close()

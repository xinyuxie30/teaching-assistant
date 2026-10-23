"""Independent real-API worker used only by verify_harness_recovery_real.py."""

import argparse
import asyncio
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from pydantic import SecretStr
from real_recovery_guard import SharedCallGuard
from runtime_stress_support import REPO, test_schema, test_url
from teaching_assistant.db import AgentRun, Database
from teaching_assistant.harness import budgets, pool
from teaching_assistant.harness.models import DeepSeekAgentModel, build_model
from teaching_assistant.harness.worker import HarnessWorker
from teaching_assistant.settings import Settings
from verify_harness_real import (
    CACHE_PRICE,
    INPUT_PRICE,
    MODEL,
    OUTPUT_PRICE,
    PRICE_VERSION,
    require,
)


def settings_for(url, *, offline=False):
    test_url(url)
    return Settings(
        _env_file=REPO / ".env",
        database_url=url,
        dev_api_token="synthetic-real-recovery-token",
        jev_api_key=None,
        jev_model=None,
        **({"deepseek_api_key": SecretStr("offline-fixture-only")} if offline else {}),
        deepseek_model=MODEL,
        deepseek_expected_model=MODEL,
        deepseek_max_output_tokens=2048,
        deepseek_input_usd_per_million=INPUT_PRICE,
        deepseek_output_usd_per_million=OUTPUT_PRICE,
        deepseek_cached_input_usd_per_million=CACHE_PRICE,
        deepseek_price_version=PRICE_VERSION,
        deepseek_price_source=budgets.PRICE_SOURCE,
        call_timeout_seconds=2 if offline else 30,
        lease_seconds=5 if offline else 35,
        poll_seconds=0.1,
        retry_delay_seconds=0,
        harness_hook_id="deny-tools",
        harness_hook_config={},
    )


def sent_trace(callback):
    """A proxy CONNECT is not a model dispatch; observe only the actual POST."""
    is_model_post = False
    emitted = False

    async def trace(event, info):
        nonlocal is_model_post, emitted
        if event == "http11.send_request_body.started":
            request = info["request"]
            is_model_post = (
                request.method == b"POST"
                and request.url.host == b"api.deepseek.com"
                and request.url.target in {b"/chat/completions", b"/v1/chat/completions"}
            )
        if event == "http11.send_request_body.complete" and is_model_post and not emitted:
            emitted = True
            await callback()

    return trace


async def main(args):
    url = os.environ["TA_REAL_DRILL_DATABASE_URL"]
    test_schema(args.schema)
    settings = settings_for(url, offline=args.offline)
    require(bool(settings.deepseek_api_key), "deepseek_key_required")
    db = Database(url, schema=args.schema)
    guard = SharedCallGuard(args.directory / "calls.sqlite3")
    require(guard.snapshot()["offline"] == args.offline, "guard_mode_mismatch")

    def config():
        return json.loads((args.directory / "control.json").read_text(encoding="utf-8"))

    class RealWorker(HarnessWorker):
        def __init__(self, *values, **options):
            super().__init__(*values, **options)
            self.owner = f"{args.name}-{uuid4().hex}"
            self.item, self.active_lease, self.task_type, self.root_id = None, None, None, None

        async def claim(self):
            self.run_ids = tuple(UUID(value) for value in config()["roots"])
            return await super().claim()

        async def gate(self, phase, *, tools=()):
            for gate in config()["gates"]:
                if (
                    gate["phase"] != phase
                    or gate["root_id"] != self.root_id
                    or gate.get("task_type", self.task_type) != self.task_type
                    or (gate.get("tool") and gate["tool"] not in tools)
                ):
                    continue
                details = {
                    "phase": phase,
                    "worker": args.name,
                    "run_id": str(self.active_lease.run_id),
                    "epoch": self.active_lease.epoch,
                    "owner": self.active_lease.owner,
                    "sequence": self.item["sequence"] if self.item else None,
                }
                if await guard.gate_once(gate["name"], **details):
                    path = args.directory / f"{gate['name']}.release"
                    while not path.exists():  # noqa: ASYNC110 - controller release signal
                        await asyncio.sleep(0.05)

        async def invoke(self, lease, body, policy, timeout_seconds):
            self.active_lease = lease
            async with db.sessions() as session:
                row = await session.get(AgentRun, lease.run_id)
                self.task_type, self.root_id = row.task_type, str(row.root_run_id or row.id)
                sequence = row.inflight_seq
            self.item = await guard.reserve(lease.run_id, sequence, body, policy, args.name)
            try:
                reply = await super().invoke(lease, body, policy, timeout_seconds)
            except BaseException as exc:
                await guard.finish(self.item, error=exc)
                raise
            await guard.finish(self.item, reply=reply)
            await self.gate("response_received")
            return reply

        def build_model(self, policy):
            owner = self

            class ObservedModel:
                async def respond(self, body, manifest):
                    async def sent():
                        await guard.update(owner.item, sent_at=datetime.now(UTC).isoformat())
                        await owner.gate("request_sent")

                    if args.offline:
                        await sent()
                        await asyncio.sleep(0.15)
                        reply = await build_model(
                            settings, {**manifest, "provider": "fake"}
                        ).respond(body, manifest)
                        return reply.model_copy(
                            update={
                                "metadata": reply.metadata.model_copy(
                                    update={
                                        "usage": {"input_tokens": 32, "output_tokens": 16},
                                        "provider_request_id": "offline-"
                                        + str(owner.item["ordinal"]),
                                    }
                                )
                            }
                        )

                    async def observe(request):
                        # No request/response content or headers are written to logs.
                        request.extensions["trace"] = sent_trace(sent)

                    async with httpx.AsyncClient(
                        proxy=settings.deepseek_proxy,
                        trust_env=False,
                        event_hooks={"request": [observe]},
                    ) as client:
                        return await DeepSeekAgentModel(settings, client=client).respond(
                            body, manifest
                        )

            return ObservedModel()

        async def save_reply(self, lease, reply):
            result = await super().save_reply(lease, reply)
            await self.gate(
                "response_saved", tools=[call.function.name for call in reply.tool_calls]
            )
            return result

    pool.HarnessWorker = RealWorker
    execution = asyncio.create_task(pool.run_pool(db, settings, concurrency=1))

    async def stop_requested():
        while not args.stop.exists():  # noqa: ASYNC110 - controller stop signal
            await asyncio.sleep(0.1)

    stop = asyncio.create_task(stop_requested())
    try:
        done, _ = await asyncio.wait((execution, stop), return_when=asyncio.FIRST_COMPLETED)
        if execution in done:
            await execution
    finally:
        execution.cancel()
        stop.cancel()
        await asyncio.gather(execution, stop, return_exceptions=True)
        await db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--stop", type=Path, required=True)
    parser.add_argument("--offline", action="store_true")
    logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(message)s")
    asyncio.run(main(parser.parse_args()))

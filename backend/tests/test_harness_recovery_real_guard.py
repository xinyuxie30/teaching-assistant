"""The opt-in recovery runner must retain paid-call caps across process death."""

import asyncio
import json
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

TOOLS = Path(__file__).parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
from real_recovery_guard import SharedCallGuard  # noqa: E402
from real_recovery_worker import sent_trace  # noqa: E402
from test_harness_real_guard import BODY, policy  # noqa: E402
from verify_harness_real import ModelError, VerificationError  # noqa: E402


def reply(usage):
    return SimpleNamespace(metadata=SimpleNamespace(model_dump=lambda **_: {"usage": usage}))


async def test_proxy_connect_does_not_trigger_request_sent_barrier():
    sent = []

    async def callback():
        sent.append(True)

    trace = sent_trace(callback)
    for method, host, target in (
        (b"CONNECT", b"localhost", b"api.deepseek.com:443"),
        (b"POST", b"unrelated.invalid", b"/chat/completions"),
        (b"POST", b"api.deepseek.com", b"/chat/completions"),
    ):
        await trace(
            "http11.send_request_body.started",
            {
                "request": SimpleNamespace(
                    method=method, url=SimpleNamespace(host=host, target=target)
                )
            },
        )
        await trace("http11.send_request_body.complete", {"return_value": None})
        assert len(sent) == int(host == b"api.deepseek.com")
    await trace("http11.send_request_body.complete", {"return_value": None})
    assert len(sent) == 1


async def test_reopening_guard_keeps_unknown_call_and_refuses_duplicate_dispatch(tmp_path):
    path = tmp_path / "calls.sqlite3"
    guard = SharedCallGuard.create(path, max_calls=2, max_cost_usd=Decimal("0.30"))
    run_id = uuid4()
    item = await guard.reserve(run_id, 3, BODY, policy(), "worker-a")
    reopened = SharedCallGuard(path)
    with pytest.raises(ModelError) as caught:
        await reopened.reserve(run_id, 3, BODY, policy(), "worker-b")
    assert caught.value.code == "real_verification_duplicate_dispatch"
    snapshot = reopened.snapshot()
    assert snapshot["calls_started"] == 1
    assert snapshot["accounted_peak_cost_usd"] == item["reserved_cost_usd"]
    assert snapshot["calls"][0]["status"] == "reserved"
    retry = await reopened.reserve(run_id, 8, BODY, policy(), "worker-b")
    await reopened.finish(retry, reply=reply({"input_tokens": 10, "output_tokens": 5}))
    with pytest.raises(ModelError) as caught:
        await guard.reserve(run_id, 12, BODY, policy(), "worker-c")
    assert caught.value.code == "real_verification_call_cap"
    assert guard.snapshot()["calls_started"] == 2


@pytest.mark.parametrize("failure", [False, True])
async def test_unknown_receipt_never_releases_reserved_cost(tmp_path, failure):
    guard = SharedCallGuard.create(
        tmp_path / "calls.sqlite3", max_calls=2, max_cost_usd=Decimal("0.30")
    )
    item = await guard.reserve(uuid4(), 3, BODY, policy(), "worker-a")
    before = guard.snapshot()["accounted_peak_cost_usd"]
    if failure:
        await guard.finish(item, error=TimeoutError())
    else:
        await guard.finish(item, reply=reply({}))
    assert guard.snapshot()["accounted_peak_cost_usd"] == before
    assert not guard.snapshot()["calls"][0]["usage_complete"]


async def test_known_receipt_is_immutable_and_gates_fire_once_across_reopens(tmp_path):
    path = tmp_path / "calls.sqlite3"
    guard = SharedCallGuard.create(path, max_calls=2, max_cost_usd=Decimal("0.30"))
    item = await guard.reserve(uuid4(), 3, BODY, policy(), "worker-a")
    await guard.finish(item, reply=reply({"input_tokens": 10, "output_tokens": 5}))
    reopened = SharedCallGuard(path)
    with pytest.raises(VerificationError, match="guard_receipt_conflict"):
        await reopened.finish(item, reply=reply({"input_tokens": 0, "output_tokens": 0}))
    assert await guard.gate_once("test", worker="a")
    assert not await reopened.gate_once("test", worker="b")
    assert reopened.snapshot()["gates"]["test"] == {"worker": "a"}
    with pytest.raises(FileExistsError):
        SharedCallGuard.create(path, max_calls=48, max_cost_usd=Decimal("1"))


@pytest.mark.parametrize("limit", ["calls", "cost"])
async def test_independent_processes_cannot_share_the_same_capacity_twice(tmp_path, limit):
    path = tmp_path / "calls.sqlite3"
    guard = SharedCallGuard.create(
        path,
        max_calls=2 if limit == "calls" else 6,
        max_cost_usd=Decimal("0.30") if limit == "calls" else Decimal("0.01"),
    )
    body = (
        BODY
        if limit == "calls"
        else {**BODY, "messages": [{"role": "user", "content": "x" * 10000}]}
    )
    payload = tmp_path / "request.json"
    payload.write_text(json.dumps({"body": body, "policy": policy()}), encoding="utf-8")
    script = """
import asyncio, json, sys
from pathlib import Path
from uuid import uuid4
sys.path.insert(0, sys.argv[1])
from real_recovery_guard import SharedCallGuard, ModelError
async def run():
    request = json.loads(Path(sys.argv[3]).read_text(encoding='utf-8'))
    try:
        await SharedCallGuard(Path(sys.argv[2])).reserve(uuid4(), 1, request['body'], request['policy'], 'process')
        print('reserved')
    except ModelError as error:
        print(error.code)
asyncio.run(run())
"""

    async def child():
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            script,
            str(TOOLS),
            str(path),
            str(payload),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(process.communicate(), 30)
        assert process.returncode == 0, err.decode()
        return out.decode().strip()

    outcomes = await asyncio.gather(*(child() for _ in range(6)))
    assert outcomes.count("reserved") == (2 if limit == "calls" else 1)
    snapshot = guard.snapshot()
    assert snapshot["calls_started"] == outcomes.count("reserved")
    assert Decimal(snapshot["accounted_peak_cost_usd"]) <= Decimal(snapshot["max_cost_usd"])

"""Test-only, cross-process paid-call cap; independent of the faulted PostgreSQL link.

SQLite is solely a local acceptance-run safety ledger, never application storage.
Every outbound attempt consumes a durable slot before HTTP. Uncertain attempts
retain their full reservation across process death and ledger reopens.
"""

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal

from verify_harness_real import (
    INPUT_PRICE,
    OUTPUT_PRICE,
    CallGuard,
    ModelError,
    complete_usage,
    require,
    safe_error,
)


class SharedCallGuard:
    def __init__(self, path):
        self.path = path

    @classmethod
    def create(cls, path, *, max_calls, max_cost_usd, offline=False):
        require(1 <= max_calls <= 48, "invalid_global_call_limit")
        require(
            max_cost_usd.is_finite() and Decimal("0.01") <= max_cost_usd <= Decimal("1"),
            "invalid_global_cost_limit",
        )
        with path.open("xb"):
            pass
        with sqlite3.connect(path) as connection:
            connection.executescript(
                "CREATE TABLE config (max_calls INTEGER, max_cost TEXT, offline INTEGER);"
                "CREATE TABLE calls (id INTEGER PRIMARY KEY, run_id TEXT, sequence INTEGER, "
                "body TEXT, UNIQUE(run_id, sequence));"
                "CREATE TABLE gates (name TEXT PRIMARY KEY, body TEXT);"
                "CREATE TABLE denials (code TEXT);"
            )
            connection.execute(
                "INSERT INTO config VALUES (?, ?, ?)",
                (max_calls, str(max_cost_usd), int(offline)),
            )
        return cls(path)

    def transaction(self, action):
        # mode=rw prevents a typo or lost ledger from silently creating a new cap.
        connection = sqlite3.connect(
            self.path.resolve().as_uri() + "?mode=rw", uri=True, timeout=10
        )
        try:
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            value = action(connection)
            connection.commit()
            return value
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def items(connection):
        return [
            json.loads(row[0]) for row in connection.execute("SELECT body FROM calls ORDER BY id")
        ]

    async def reserve(self, run_id, sequence, body, policy, worker):
        # Reuse the official-endpoint/model and conservative bound validation.
        quote = await CallGuard(1, Decimal("1")).reserve(run_id, body, policy)

        def action(connection):
            max_calls, max_cost, _ = connection.execute("SELECT * FROM config").fetchone()
            calls = self.items(connection)
            duplicate = any(
                item["run_id"] == str(run_id) and item["sequence"] == sequence for item in calls
            )
            accounted = sum((Decimal(item["accounted_cost_usd"]) for item in calls), Decimal(0))
            code = (
                "real_verification_duplicate_dispatch"
                if duplicate
                else "real_verification_call_cap"
                if len(calls) >= max_calls
                else "real_verification_cost_cap"
                if accounted + Decimal(quote["reserved_cost_usd"]) > Decimal(max_cost)
                else None
            )
            if code:
                connection.execute("INSERT INTO denials VALUES (?)", (code,))
                return code
            item = {**quote, "ordinal": len(calls) + 1, "sequence": sequence, "worker": worker}
            connection.execute(
                "INSERT INTO calls VALUES (?, ?, ?, ?)",
                (item["ordinal"], str(run_id), sequence, json.dumps(item)),
            )
            return item

        result = await asyncio.to_thread(self.transaction, action)
        if isinstance(result, str):
            raise ModelError(result, retryable=False)
        return result

    async def update(self, item, **fields):
        def action(connection):
            row = connection.execute(
                "SELECT body FROM calls WHERE id=?", (item["ordinal"],)
            ).fetchone()
            require(row is not None, "guard_call_missing")
            current = json.loads(row[0])
            require(
                current["run_id"] == item["run_id"] and current["sequence"] == item["sequence"],
                "guard_call_identity_mismatch",
            )
            if current.get("ended_at"):
                require(
                    all(
                        current.get(key) == value
                        for key, value in fields.items()
                        if key != "ended_at"
                    ),
                    "guard_receipt_conflict",
                )
                return current
            current.update(fields)
            connection.execute(
                "UPDATE calls SET body=? WHERE id=?", (json.dumps(current), item["ordinal"])
            )
            return current

        return await asyncio.to_thread(self.transaction, action)

    async def finish(self, item, *, reply=None, error=None):
        metadata = (
            reply.metadata.model_dump(mode="json")
            if reply is not None
            else {
                key: getattr(error, key, None)
                for key in ("usage", "provider_request_id", "returned_model", "http_status")
            }
        )
        usage = metadata.get("usage")
        known = complete_usage(usage)
        amount = Decimal(item["reserved_cost_usd"])
        if known:
            amount = (
                (
                    Decimal(usage["input_tokens"]) * INPUT_PRICE
                    + Decimal(usage["output_tokens"]) * OUTPUT_PRICE
                )
                / Decimal(1000000)
            ).quantize(Decimal("0.000000000001"), rounding=ROUND_CEILING)
        result = await self.update(
            item,
            metadata=metadata,
            usage_complete=known,
            status="succeeded" if reply is not None else "failed",
            error=None if error is None else safe_error(error),
            accounted_cost_usd=str(amount),
            ended_at=datetime.now(UTC).isoformat(),
        )
        if amount > Decimal(item["reserved_cost_usd"]):
            raise ModelError("real_verification_cost_overrun", retryable=False)
        return result

    async def gate_once(self, name, **fields):
        def action(connection):
            return (
                connection.execute(
                    "INSERT OR IGNORE INTO gates VALUES (?, ?)", (name, json.dumps(fields))
                ).rowcount
                == 1
            )

        return await asyncio.to_thread(self.transaction, action)

    def snapshot(self):
        def action(connection):
            max_calls, max_cost, offline = connection.execute("SELECT * FROM config").fetchone()
            calls = self.items(connection)
            return {
                "max_calls": max_calls,
                "max_cost_usd": max_cost,
                "offline": bool(offline),
                "calls_started": len(calls),
                "accounted_peak_cost_usd": str(
                    sum((Decimal(item["accounted_cost_usd"]) for item in calls), Decimal(0))
                ),
                "calls": calls,
                "denials": [row[0] for row in connection.execute("SELECT code FROM denials")],
                "gates": {
                    name: json.loads(body)
                    for name, body in connection.execute("SELECT * FROM gates")
                },
            }

        return self.transaction(action)

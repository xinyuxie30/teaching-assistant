import argparse
import asyncio
import json
import logging
import sys
from decimal import Decimal

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from teaching_assistant.db import Database, Teacher
from teaching_assistant.domain import DomainError, text_digest
from teaching_assistant.harness.pool import run_pool
from teaching_assistant.profiles import ProfileRegistry
from teaching_assistant.service import audit
from teaching_assistant.settings import Settings
from teaching_assistant.worker import Worker


async def bootstrap(db: Database, settings: Settings, name: str = "开发老师") -> dict:
    token = settings.dev_api_token
    if len(token) < 20 or token == "replace-with-your-own-local-token":
        raise ValueError("Set TA_DEV_API_TOKEN to your own token (at least 20 characters)")
    async with db.sessions.begin() as session:
        token_hash = text_digest(token)
        await session.execute(
            insert(Teacher)
            .values(name=name, token_digest=token_hash)
            .on_conflict_do_nothing(index_elements=["token_digest"])
        )
        teacher_id = await session.scalar(
            select(Teacher.id).where(Teacher.token_digest == token_hash)
        )
    return {"teacher_id": str(teacher_id), "identity_mode": "local-development-token"}


async def rotate_token(db: Database, settings: Settings, name: str = "开发老师") -> dict:
    """Replace one teacher's development token without creating a second identity."""
    token = settings.dev_api_token
    if len(token) < 20 or token == "replace-with-your-own-local-token":
        raise ValueError("Set TA_DEV_API_TOKEN to the new token (at least 20 characters)")
    digest = text_digest(token)
    async with db.sessions.begin() as session:
        teachers = list(await session.scalars(select(Teacher).where(Teacher.name == name)))
        if len(teachers) != 1:
            raise ValueError(f"Expected exactly one teacher named {name!r}, found {len(teachers)}")
        teacher = teachers[0]
        if teacher.token_digest == digest:
            return {"teacher_id": str(teacher.id), "rotated": False, "reason": "already_current"}
        if await session.scalar(select(Teacher.id).where(Teacher.token_digest == digest)):
            raise ValueError("The new token is already assigned to another teacher")
        teacher.token_digest = digest
        audit(session, teacher.id, "developer_token_rotated", teacher.id, name=name)
    return {
        "teacher_id": str(teacher.id),
        "rotated": True,
        "identity_mode": "local-development-token",
    }


async def run(args) -> None:
    settings = Settings()
    if args.command == "profiles":
        print(json.dumps(ProfileRegistry(settings).public(), ensure_ascii=False, indent=2))
        return
    if args.command == "compare":
        from teaching_assistant.comparison import run_comparison

        result = await run_comparison(settings, args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    db = Database(settings.database_url)
    try:
        if args.command == "bootstrap":
            print(json.dumps(await bootstrap(db, settings, args.name), ensure_ascii=False))
        elif args.command == "rotate-token":
            print(json.dumps(await rotate_token(db, settings, args.name), ensure_ascii=False))
        elif args.command in {"agent-worker", "harness-worker"}:
            mode = "once" if args.once else "drain" if args.drain else "forever"
            processed = await run_pool(db, settings, concurrency=args.concurrency, mode=mode)
            if mode != "forever":
                print(json.dumps({"processed": processed, "concurrency": args.concurrency}))
        elif args.command == "worker":
            worker = Worker(db, settings)
            if args.once:
                print(json.dumps({"processed": await worker.run_once()}))
            elif args.drain:
                processed = 0
                while await worker.run_once():
                    processed += 1
                print(
                    json.dumps(
                        {"processed": processed, "notice": "Only currently ready work drained"}
                    )
                )
            else:
                await worker.run_forever()
        elif args.command == "demo":
            from teaching_assistant.demo import demo

            await bootstrap(db, settings)
            result = await demo(db, settings, args.run_id, args.output)
            print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        await db.close()


def main():
    # Redirected Windows consoles can default to cp1252 even for a Chinese workspace.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Teaching assistant local development commands")
    commands = parser.add_subparsers(dest="command", required=True)
    identity = commands.add_parser(
        "bootstrap", help="Register the configured local development token"
    )
    identity.add_argument("--name", default="开发老师")
    rotate = commands.add_parser(
        "rotate-token", help="Replace one teacher's development token using TA_DEV_API_TOKEN"
    )
    rotate.add_argument("--name", default="开发老师")
    worker = commands.add_parser("worker")
    mode = worker.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--drain", action="store_true")
    agent_worker = commands.add_parser(
        "harness-worker", aliases=["agent-worker"], help="Run registered durable Harness tasks"
    )
    agent_mode = agent_worker.add_mutually_exclusive_group()
    agent_mode.add_argument("--once", action="store_true")
    agent_mode.add_argument("--drain", action="store_true")
    agent_worker.add_argument(
        "--concurrency",
        type=int,
        choices=range(1, 17),
        default=1,
        help="Independent Harness execution slots in this process (default: 1)",
    )
    demo_parser = commands.add_parser(
        "demo", help="Run synthetic API → worker → review → export flow"
    )
    demo_parser.add_argument("--run-id", default="demo-v1")
    demo_parser.add_argument("--output", default=".local/demo-result.json")
    commands.add_parser(
        "profiles", help="List locally configured model profiles without calling them"
    )
    compare = commands.add_parser("compare", help="Compare fixed samples with teacher labels")
    compare.add_argument("--dataset", required=True)
    compare.add_argument("--profiles", nargs="+", default=["fake-v1", "jev-v1"])
    compare.add_argument("--output-dir", default=".local/comparisons")
    compare.add_argument("--max-samples", type=int, default=8)
    compare.add_argument("--max-calls", type=int, default=40)
    compare.add_argument("--max-seconds", type=float, default=180)
    compare.add_argument("--max-attempts", type=int, default=1)
    compare.add_argument("--confidence-audit-threshold", type=float, default=0.9)
    compare.add_argument("--severe-error-points", type=Decimal)
    compare.add_argument("--input-usd-per-million", type=Decimal)
    compare.add_argument("--output-usd-per-million", type=Decimal)
    compare.add_argument("--cached-input-usd-per-million", type=Decimal)
    compare.add_argument("--price-source")
    compare.add_argument(
        "--pricing-file", help="JSON prices keyed by selected real profile alias or ID"
    )
    compare.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except ValidationError as exc:
        print(
            json.dumps(
                {
                    "code": "invalid_input",
                    "errors": [
                        {"location": error["loc"], "message": error["msg"], "type": error["type"]}
                        for error in exc.errors()
                    ],
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        raise SystemExit(2) from None
    except (ValueError, DomainError, OSError) as exc:
        print(
            json.dumps(
                {
                    "code": exc.code if isinstance(exc, DomainError) else "command_error",
                    "detail": str(exc),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()

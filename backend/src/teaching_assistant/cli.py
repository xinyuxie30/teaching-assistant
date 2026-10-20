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
from teaching_assistant.profiles import ProfileRegistry
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
    worker = commands.add_parser("worker")
    mode = worker.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--drain", action="store_true")
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
    compare.add_argument("--price-source")
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

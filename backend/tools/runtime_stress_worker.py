"""Real subprocess entry point; synthetic model delays and crash barriers only."""

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
from uuid import uuid4

from runtime_stress_support import settings_for, test_schema, test_url
from teaching_assistant.db import Database
from teaching_assistant.evaluators import FakeEvaluator, FakeEvidenceExtractor
from teaching_assistant.harness import pool
from teaching_assistant.harness.worker import HarnessWorker
from teaching_assistant.worker import Worker


async def main(args):
    url = os.environ["TA_STRESS_DATABASE_URL"]
    test_url(url)
    test_schema(args.schema)
    settings = settings_for(url)
    db = Database(url, schema=args.schema)

    async def barrier(kind, **fields):
        if args.gate == kind:
            args.signal.write_text(json.dumps({"kind": kind, **fields}), encoding="utf-8")
            await asyncio.Event().wait()

    class DrillHarness(HarnessWorker):
        def __init__(self, *values, **options):
            super().__init__(*values, **options)
            self.owner = f"{args.name}-{uuid4().hex}"
            self.active_lease = None

        async def execute(self, lease):
            self.active_lease = lease
            return await super().execute(lease)

        def build_model(self, policy):
            if policy["provider"] != "fake":
                raise RuntimeError("The drill refuses real model providers")
            delegate = super().build_model(policy)
            owner = self

            class DelayedModel:
                async def respond(self, body, manifest):
                    if manifest.get("task_type") == "lesson-practice-designer":
                        await barrier("designer_model", run_id=str(owner.active_lease.run_id))
                    await barrier("model", run_id=str(owner.active_lease.run_id))
                    await asyncio.sleep(0.15)
                    reply = await delegate.respond(body, manifest)
                    # Known, bounded fixture usage lets the audit distinguish lost
                    # responses from settled successes without a paid provider.
                    return reply.model_copy(
                        update={
                            "metadata": reply.metadata.model_copy(
                                update={"usage": {"input_tokens": 32, "output_tokens": 16}}
                            )
                        }
                    )

            return DelayedModel()

        async def finish_step(self, session, run):
            await super().finish_step(session, run)
            if run.status == "waiting_teacher":
                await barrier("tool_commit", run_id=str(run.id))

    class DelayedEvaluator(FakeEvaluator):
        async def assess(self, request):
            await asyncio.sleep(0.15)
            return await super().assess(request)

    class DelayedExtractor(FakeEvidenceExtractor):
        async def extract(self, request):
            await barrier("grading_evidence", answer_id=str(request.answer_version_id))
            await asyncio.sleep(0.15)
            return await super().extract(request)

    pool.HarnessWorker = DrillHarness
    execution = asyncio.create_task(
        pool.run_pool(db, settings, concurrency=args.slots)
        if args.kind == "harness"
        else Worker(
            db,
            settings,
            evaluator=DelayedEvaluator(),
            extractor=DelayedExtractor(),
            worker_id=args.name,
        ).run_forever()
    )

    async def stop_requested():
        while not args.stop.exists():  # noqa: ASYNC110 - cross-process filesystem signal
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
    parser.add_argument("--kind", choices=("harness", "grading"), required=True)
    parser.add_argument("--slots", type=int, choices=range(1, 17), default=1)
    parser.add_argument(
        "--gate", choices=("model", "designer_model", "tool_commit", "grading_evidence")
    )
    parser.add_argument("--stop", type=Path, required=True)
    parser.add_argument("--signal", type=Path, required=True)
    logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(message)s")
    asyncio.run(main(parser.parse_args()))

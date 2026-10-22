"""Transactional event receipts and termination shared by every task."""

from teaching_assistant.db import AgentEvent
from teaching_assistant.harness.context import tool_result


def event(session, run, kind, **body):
    run.event_seq += 1
    session.add(
        AgentEvent(run_id=run.id, sequence=run.event_seq, epoch=run.epoch, kind=kind, body=body)
    )
    return run.event_seq


def close_inflight(session, run, code):
    if run.inflight_seq is not None:
        event(
            session,
            run,
            "model_unknown",
            call_sequence=run.inflight_seq,
            error_code=code,
            usage=None,
        )
        run.inflight_seq = None


def close_pending(session, run, code):
    for call in run.pending_calls:
        run.messages = [*run.messages, tool_result(call["id"], {"ok": False, "error_code": code})]
        event(session, run, "tool_skipped", tool_call_id=call["id"], error_code=code)
    run.pending_calls = []


def finish(session, run, status, code=None):
    close_inflight(session, run, code or status)
    close_pending(session, run, code or status)
    run.status, run.last_error = status, code
    run.lease_owner, run.lease_until = None, None
    event(session, run, status, error_code=code)

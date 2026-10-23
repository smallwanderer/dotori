"""Shared execution context and validation; domain snapshots are callbacks."""
from contextvars import ContextVar
import time

active_execution = ContextVar("active_executor_execution", default=None)
active_snapshot_reader = ContextVar("active_executor_snapshot_reader", default=None)


class StaleExecution(ValueError):
    """Stop without overwriting newer domain state."""


def validate_execution(snapshot_reader=None):
    context = active_execution.get()
    if context is None:
        return
    envelope, expected_source, expected_runtime = context
    if envelope.get("plan_id") or envelope.get("step_id"):
        from orchestration.submissions import authorize_if_planned
        authorize_if_planned(envelope)
    if time.time() >= float(envelope.get("deadline_at", time.time() + 1800)):
        raise StaleExecution("Executor deadline exceeded before persistence")
    reader = snapshot_reader or active_snapshot_reader.get()
    if reader is None:
        raise StaleExecution("Executor snapshot reader is missing")
    source, runtime = reader(envelope)
    if source != expected_source or runtime != expected_runtime:
        raise StaleExecution("Executor input or runtime changed before persistence")

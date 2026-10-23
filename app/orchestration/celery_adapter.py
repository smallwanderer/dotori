"""Celery delivery/retry coordination, independent of domain algorithms."""
import logging
import random
import threading
import time

from capabilities.admission import CapabilityAdmissionError, require_capability
from capabilities.work_registry import WORK_REGISTRY, get_work_definition, require_registered_work
from orchestration.contracts import WorkContractError, validate_followups
from orchestration.dispatch import publish_registered_work
from orchestration.transport import ExecutorDispatchError, dispatch_executor_job, read_executor_job

logger = logging.getLogger(__name__)
_DISPATCH_SLOTS = {
    queue: threading.BoundedSemaphore(1)
    for queue in {definition.queue for definition in WORK_REGISTRY.values()}
}

def executor_envelope(
    *,
    job_id: str | None,
    parent_job_id: str | None,
    trace_id: str | None,
    enqueued_at: str | None,
    work_kind: str | None,
    envelope_version: int | None,
) -> dict:
    from orchestration.contracts import TASK_ENVELOPE_VERSION
    from config.tracing import get_trace_id, new_trace_id

    if not job_id:
        from celery import current_task
        from uuid import uuid4

        # Messages published before envelope v1 carry Celery's immutable task
        # id. Direct compatibility calls have no request object, so they get a
        # one-off identity only for that local invocation.
        job_id = getattr(getattr(current_task, "request", None), "id", None) or str(uuid4())
    return {
        "job_id": job_id,
        "parent_job_id": parent_job_id,
        "trace_id": trace_id or get_trace_id() or new_trace_id(),
        "enqueued_at": enqueued_at,
        "work_kind": work_kind,
        "envelope_version": envelope_version or TASK_ENVELOPE_VERSION,
    }


def publish_executor_followups(response: dict, *, publish=None) -> None:
    publish = publish or publish_registered_work
    followups = response.get("followups", [])
    validate_followups(followups)
    for followup in followups:
        publish(
            followup["work_kind"], followup["payload"],
            job_id=followup["job_id"],
            parent_job_id=followup.get("parent_job_id"),
        )


def dispatch_or_retry(task, kind: str, *, envelope: dict, payload: dict,
                      on_rejected=None, dispatch=None, publish_followups=None, read_receipt=None) -> dict:
    dispatch = dispatch or dispatch_executor_job
    publish_followups = publish_followups or publish_executor_followups
    headers = dict(getattr(task.request, "headers", None) or {})
    deadline = headers.setdefault("executor_deadline", time.time() + 1800)
    envelope = {**envelope, "deadline_at": deadline, "attempt": (getattr(task.request, "retries", 0) or 0) + 1}
    redelivered = (envelope["attempt"] > 1 or bool(
        (getattr(task.request, "delivery_info", None) or {}).get("redelivered")))
    plan_adapter = None

    def retry(exc, delay=5):
        if time.time() >= deadline:
            raise ExecutorDispatchError("Executor deadline exceeded; inspect receipt before recovery", retryable=False) from exc
        raise task.retry(exc=exc, countdown=min(delay + random.uniform(0, 1), max(0, deadline - time.time())),
                         headers=headers, max_retries=100000)

    def record(response):
        try:
            if plan_adapter is not None:
                plan_adapter.record_response(str(kind), envelope, payload, response)
            else:
                publish_followups(response)
        except WorkContractError as exc:
            logger.error("Executor returned an invalid followup: %s", exc)
            return {"status": "failed", "error_code": "INVALID_PLAN_RESULT" if plan_adapter else "INVALID_FOLLOWUP", "error": str(exc), "retryable": False}
        except CapabilityAdmissionError as exc:
            if not exc.retryable:
                return {"status": "failed", "error_code": exc.code, "error": str(exc), "retryable": False}
            retry(exc)
        except Exception as exc:
            retry(exc)
        return response.get("result") or {"status": "failed" if response.get("status") == "failed" else "success"}

    if "plan_id" in headers or "step_id" in headers:
        envelope = {**envelope, "plan_id": headers.get("plan_id"), "step_id": headers.get("step_id")}
        try:
            from orchestration.submissions import DELIVERY
            plan_adapter = DELIVERY
            if redelivered:
                plan_adapter.authorize_report(str(kind), envelope, payload)
                try:
                    existing = (read_receipt or read_executor_job)(str(kind), envelope["job_id"])
                except ExecutorDispatchError as exc:
                    if exc.retryable and time.time() < deadline:
                        retry(exc, exc.retry_after_seconds or 5)
                    return {"status": "failed", "error": str(exc), "retryable": False}
                # Retryable failures still need current execution admission.
                if existing.get("status") == "succeeded" or (
                        existing.get("status") == "failed" and existing.get("retryable") is False):
                    if (existing.get("job_id") != envelope["job_id"]
                            or existing.get("work_kind") != str(kind)):
                        raise WorkContractError("Receipt lookup returned another job or work kind")
                    return record(existing)
            plan_adapter.authorize(str(kind), envelope, payload)
        except ValueError as exc:
            return {"status": "failed", "error_code": "INVALID_PLAN_COMMAND", "error": str(exc), "retryable": False}
    try:
        require_capability(get_work_definition(str(kind)).capability_id)
        try:
            require_registered_work(str(kind))
        except ValueError as exc:
            raise CapabilityAdmissionError(
                get_work_definition(str(kind)).capability_id, "CAPABILITY_UNAVAILABLE", str(exc), retryable=True
            ) from exc
    except CapabilityAdmissionError as exc:
        if on_rejected is not None:
            on_rejected(str(kind), payload, error_code=exc.code, message=str(exc))
        return {
            "status": "failed",
            "error_code": exc.code,
            "error": str(exc),
            "retryable": False,
        }

    if time.time() >= deadline:
        return {"status": "failed", "error": "Executor deadline exceeded; inspect receipt before recovery"}

    slot = _DISPATCH_SLOTS[get_work_definition(str(kind)).queue]
    if not slot.acquire(blocking=False):
        retry(ExecutorDispatchError("Transport slot busy"))
    try:
        try:
            response = dispatch(kind, envelope=envelope, payload=payload)
        except ExecutorDispatchError as exc:
            if exc.retryable:
                retry(exc, exc.retry_after_seconds or 5)
            return {"status": "failed", "error": str(exc)}
        return record(response)
    finally:
        slot.release()

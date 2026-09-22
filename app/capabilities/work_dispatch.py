"""Common durable-work identity and publication for registered capabilities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from django.utils import timezone

from capabilities.admission import CapabilityAdmissionError, require_capability
from capabilities.work_registry import get_work_definition, require_registered_work
from config.tracing import get_trace_id, new_trace_id


TASK_ENVELOPE_VERSION = 1


class WorkContractError(ValueError):
    """Permanent mismatch between a published followup and its work contract."""


@dataclass(frozen=True)
class TaskEnvelope:
    version: int
    job_id: str
    parent_job_id: str | None
    trace_id: str
    work_kind: str
    enqueued_at: str

    @classmethod
    def create(
        cls,
        kind: str,
        *,
        job_id: str | None = None,
        parent_job_id: str | None = None,
        trace_id: str | None = None,
        enqueued_at: str | None = None,
    ) -> TaskEnvelope:
        work_kind = str(kind)
        get_work_definition(work_kind)
        return cls(
            version=TASK_ENVELOPE_VERSION,
            job_id=job_id or str(uuid4()),
            parent_job_id=parent_job_id,
            trace_id=trace_id or get_trace_id() or new_trace_id(),
            work_kind=work_kind,
            enqueued_at=enqueued_at or timezone.now().isoformat(),
        )

    def as_task_kwargs(self) -> dict[str, Any]:
        return {
            "envelope_version": self.version,
            "job_id": self.job_id,
            "parent_job_id": self.parent_job_id,
            "trace_id": self.trace_id,
            "work_kind": self.work_kind,
            "enqueued_at": self.enqueued_at,
        }


def derive_job_id(parent_job_id: str, kind: str, target: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"dotori:{parent_job_id}:{str(kind)}:{target}"))


def validate_work_payload(kind: str, payload: dict[str, Any]):
    try:
        definition = get_work_definition(kind)
    except ValueError as exc:
        raise WorkContractError(str(exc)) from exc
    if not isinstance(payload, dict) or set(payload) != set(definition.argument_fields):
        raise WorkContractError(f"Invalid payload fields for {kind}")
    return definition


def validate_followups(followups: Any) -> None:
    if not isinstance(followups, list):
        raise WorkContractError("Executor followups must be a list")
    for followup in followups:
        if not isinstance(followup, dict):
            raise WorkContractError("Invalid executor followup envelope")
        try:
            validate_work_payload(followup["work_kind"], followup["payload"])
            job_id = followup["job_id"]
        except (KeyError, TypeError) as exc:
            raise WorkContractError("Invalid executor followup envelope") from exc
        if not isinstance(job_id, str) or not job_id or len(job_id) > 64:
            raise WorkContractError("Followup job_id must be a string up to 64 characters")


def publish_registered_work(kind: str, payload: dict[str, Any], **kwargs: Any):
    """Publish an executor followup without importing a modality module."""
    from celery import current_app

    definition = validate_work_payload(kind, payload)
    require_capability(definition.capability_id)
    try:
        require_registered_work(kind)
    except ValueError as exc:
        raise CapabilityAdmissionError(
            definition.capability_id, "CAPABILITY_UNAVAILABLE", str(exc), retryable=True
        ) from exc
    envelope = TaskEnvelope.create(kind, **kwargs)
    return current_app.send_task(
        definition.task_name,
        args=[payload[field] for field in definition.argument_fields],
        kwargs=envelope.as_task_kwargs(),
        queue=definition.queue,
        task_id=envelope.job_id,
    )

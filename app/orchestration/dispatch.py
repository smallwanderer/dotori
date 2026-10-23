"""Publication and admission for registered work; no modality imports."""
from typing import Any
from capabilities.admission import CapabilityAdmissionError, require_capability
from capabilities.work_registry import get_work_definition, require_registered_work
from orchestration.contracts import TaskEnvelope, TASK_ENVELOPE_VERSION, validate_work_payload


def admitted_work(kind: str):
    definition = get_work_definition(kind)
    require_capability(definition.capability_id)
    try:
        require_registered_work(kind)
    except ValueError as exc:
        raise CapabilityAdmissionError(
            definition.capability_id, "CAPABILITY_UNAVAILABLE", str(exc), retryable=True
        ) from exc
    return definition


def dispatch_task(task, kind: str, *, args: list[Any], kwargs: dict[str, Any], headers=None):
    """Keep task-object publication and extra kwargs for legacy producers."""
    definition = admitted_work(kind)
    kwargs = dict(kwargs)
    keys = {"job_id", "parent_job_id", "trace_id", "enqueued_at", "work_kind", "envelope_version"}
    values = {key: kwargs.pop(key) for key in tuple(kwargs) if key in keys}
    supplied_kind = values.get("work_kind")
    if supplied_kind is not None and supplied_kind != kind:
        raise ValueError(f"work_kind {supplied_kind!r} does not match dispatch kind {kind!r}")
    version = values.get("envelope_version")
    if version is not None and version != TASK_ENVELOPE_VERSION:
        raise ValueError(f"Unsupported task envelope version: {version!r}")
    envelope = TaskEnvelope.create(
        kind, **{key: values.get(key) for key in ("job_id", "parent_job_id", "trace_id", "enqueued_at")}
    )
    return task.apply_async(
        args=args, kwargs={**kwargs, **envelope.as_task_kwargs()},
        queue=definition.queue, task_id=envelope.job_id,
        **({"headers": headers} if headers is not None else {}),
    )


def publish_registered_work(kind: str, payload: dict[str, Any], *, headers=None, **kwargs: Any):
    from celery import current_app

    validate_work_payload(kind, payload)
    definition = admitted_work(kind)
    envelope = TaskEnvelope.create(kind, **kwargs)
    return current_app.send_task(
        definition.task_name, args=[payload[field] for field in definition.argument_fields],
        kwargs=envelope.as_task_kwargs(), queue=definition.queue, task_id=envelope.job_id,
        **({"headers": headers} if headers is not None else {}),
    )

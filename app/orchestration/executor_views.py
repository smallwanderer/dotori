"""Private HTTP entry points for parser and embedding executor processes."""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

from django.conf import settings
from django.db import transaction
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from orchestration.contracts import TASK_ENVELOPE_VERSION
from capabilities.work_registry import get_work_definition, require_registered_work
from orchestration.contracts import validate_followups
from orchestration.receipts import (ExecutorJobConflict, ExecutionOwnershipLost,
                                    ExecutionOwnership, active_receipt_ownership, receipt_payload)
from orchestration import receipts
from orchestration.handlers import get_handler, get_receipt_repository
from orchestration.execution import active_execution, active_snapshot_reader, validate_execution



_JOB_SLOT = threading.BoundedSemaphore(1)


def _executor_role() -> str:
    return os.getenv("DOTORI_EXECUTOR_ROLE", "")


def _authorized(request) -> bool:
    token = settings.EXECUTOR_INTERNAL_TOKEN
    return bool(token) and request.headers.get("Authorization") == f"Bearer {token}"


def _error(code: str, message: str, status: int, *, retryable: bool = False):
    response = JsonResponse(
        {"ok": False, "error": {"code": code, "message": message}, "retryable": retryable},
        status=status,
    )
    if code == "EXECUTOR_BUSY":
        response["Retry-After"] = "5"
    return response


def _validate_envelope(payload: Any) -> tuple[dict[str, Any], str] | tuple[None, str]:
    if not isinstance(payload, dict):
        return None, "Expected a JSON object."
    required = ("job_id", "work_kind", "payload")
    if any(not payload.get(field) for field in required):
        return None, "job_id, work_kind, and payload are required."
    if payload.get("envelope_version") != TASK_ENVELOPE_VERSION:
        return None, "Unsupported task envelope version."
    if not isinstance(payload["payload"], dict):
        return None, "payload must be an object."
    if not isinstance(payload["job_id"], str) or len(payload["job_id"]) > 64:
        return None, "job_id must be a string up to 64 characters."
    return payload, ""


@csrf_exempt
@require_POST
def execute_job(request):
    if not _authorized(request):
        return _error("UNAUTHORIZED", "Executor token is required.", 401)
    if not _JOB_SLOT.acquire(blocking=False):
        return _error("EXECUTOR_BUSY", "Executor job slot occupied.", 503, retryable=True)
    context_token = active_execution.set(None)
    reader_token = active_snapshot_reader.set(None)
    owner_token = active_receipt_ownership.set(None)
    try:
        return _execute_job(request)
    except ExecutionOwnershipLost as exc:
        return _error("EXECUTION_OWNERSHIP_LOST", str(exc), 409)
    finally:
        active_receipt_ownership.reset(owner_token)
        active_execution.reset(context_token)
        active_snapshot_reader.reset(reader_token)
        _JOB_SLOT.release()


def _execute_job(request):
    if not _authorized(request):
        return _error("UNAUTHORIZED", "Executor token is required.", 401)
    try:
        raw_payload = json.loads(request.body or b"{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return _error("INVALID_JSON", "Malformed JSON request body.", 400)
    envelope, error = _validate_envelope(raw_payload)
    if envelope is None:
        return _error("INVALID_ENVELOPE", error, 422)
    role = _executor_role()
    try:
        definition = get_work_definition(envelope["work_kind"])
    except (ValueError, TypeError):
        return _error("UNSUPPORTED_WORK_KIND", "This executor does not own the requested work.", 422)
    if definition.execution_role != role:
        return _error("UNSUPPORTED_WORK_KIND", "This executor does not own the requested work.", 422)
    try:
        require_registered_work(envelope["work_kind"])
    except ValueError as exc:
        return _error("WORK_NOT_REGISTERED", str(exc), 422)
    try:
        from orchestration.submissions import authorize_if_planned
        authorize_if_planned(envelope)
        handler = get_handler(envelope["work_kind"], role)
        repository = get_receipt_repository(role)
        target_key = handler.target_key(envelope["payload"])
        source, runtime = handler.capture_snapshot(envelope)
        claim = receipts.claim_execution(
            repository,
            job_id=envelope["job_id"],
            work_kind=envelope["work_kind"],
            target_key=target_key,
            payload=envelope["payload"],
            source_snapshot=source,
            runtime_snapshot=runtime,
        )
    except ExecutorJobConflict as exc:
        return _error("JOB_ID_CONFLICT", str(exc), 409)
    except ValueError as exc:
        return _error("INVALID_PAYLOAD", str(exc), 422)

    if not claim.should_execute:
        body = receipt_payload(claim.receipt)
        return JsonResponse({"ok": claim.receipt.status != "failed", **body}, status=202 if claim.receipt.status == "running" else 200)

    try:
        active_receipt_ownership.set(ExecutionOwnership.capture(claim.receipt))
        if float(envelope.get("deadline_at", time.time() + 1800)) <= time.time():
            raise ValueError("Executor deadline exceeded")
        active_snapshot_reader.set(handler.capture_snapshot)
        active_execution.set((envelope, source, runtime))
        validate_execution()
        # A handler may require domain claims and durable child envelopes
        # to commit together. This policy has no modality-specific branch.
        if handler.atomic_execution:
            with transaction.atomic():
                result, followups = handler.execute(envelope)
                validate_followups(followups)
                if result.get("status") == "failed":
                    raise RuntimeError(result.get("error", "Planning failed"))
                validate_execution()
                receipt = receipts.finish_execution(repository, claim.receipt, result=result, followups=followups)
            return JsonResponse({"ok": True, **receipt_payload(receipt)})
        result, followups = handler.execute(envelope)
        validate_followups(followups)
        if getattr(handler, "manages_receipt", False) is True:
            # The owning handler commits evidence and terminal receipt together.
            # Do not revalidate mutable inputs or complete the job a second time.
            receipt = repository.get(envelope["job_id"])
            active_receipt_ownership.get().validate(receipt)
            if receipt.status not in {"succeeded", "failed"}:
                raise ValueError("Receipt-managed handler returned without terminal completion")
            return JsonResponse({"ok": receipt.status == "succeeded", **receipt_payload(receipt)},
                status=503 if receipt.status == "failed" and receipt.retryable else 200)
        validate_execution()
    except ExecutionOwnershipLost:
        raise
    except Exception as exc:  # executor failures are classified by the transport retry policy
        receipt = receipts.fail_execution(repository, claim.receipt, code="EXECUTION_FAILED", message=str(exc), retryable=not isinstance(exc, (ValueError, FileNotFoundError)))
        return JsonResponse({"ok": False, **receipt_payload(receipt)}, status=503)

    if result.get("status") == "failed":
        receipt = receipts.fail_execution(
            repository, claim.receipt,
            code="DOMAIN_FAILURE",
            message=str(result.get("error", "executor work failed")),
            retryable=False,
        )
        return JsonResponse({"ok": False, **receipt_payload(receipt)}, status=200)
    receipt = receipts.finish_execution(repository, claim.receipt, result=result, followups=followups)
    return JsonResponse({"ok": True, **receipt_payload(receipt)})


@require_GET
def job_status(request, job_id: str):
    if not _authorized(request):
        return _error("UNAUTHORIZED", "Executor token is required.", 401)
    try:
        repository = get_receipt_repository(_executor_role())
    except ValueError as exc:
        return _error("UNSUPPORTED_EXECUTOR_ROLE", str(exc), 422)
    receipt = repository.get(job_id)
    if receipt is None:
        return _error("JOB_NOT_FOUND", "No executor job receipt was found.", 404)
    return JsonResponse({"ok": True, **receipt_payload(receipt)})

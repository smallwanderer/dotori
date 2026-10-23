"""Compatibility exports for the shared executor HTTP entry points."""
import os
from django.http import JsonResponse
from django.views.decorators.http import require_GET

from orchestration.executor_views import (
    execute_job, job_status, _execute_job, _JOB_SLOT,
)


def _target_key(kind, payload):
    from documents.processing.work_handlers import _target_key as target_key
    return target_key(kind, payload)


def _execute_parser(envelope):
    from documents.processing.work_handlers import _execute_parser as execute
    return execute(envelope)


def _execute_embedding(envelope):
    from documents.processing.work_handlers import _execute_embedding as execute
    return execute(envelope)


def _executor_role():
    return os.getenv("DOTORI_EXECUTOR_ROLE", "")


@require_GET
def livez(_request):
    if "DOTORI_EXECUTOR_ROLE" not in os.environ:
        return JsonResponse({"status": "ok"})
    return JsonResponse({"status": "live", "role": _executor_role()})


@require_GET
def readyz(request):
    if _executor_role() == "parser":
        return JsonResponse({"status": "ready", "role": "parser"})
    if _executor_role() != "embedding":
        return JsonResponse({"status": "unavailable", "reason": "Unknown executor role"}, status=503)
    from documents.embedding.internal_views import readyz as embedding_readyz

    return embedding_readyz(request)

from __future__ import annotations

import json
import logging
import os
import threading

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_GET

from . import execution
from .providers.base import EmbeddingBusyError
from .registry import create_embedding_implementation as get_embedding_provider
from documents.services.embedding_runtime_config import get_active_embedding_runtime

logger = logging.getLogger(__name__)

_ALLOWED_INPUT_TYPES = {"query", "document"}
_MAX_TEXT_CHARS = 20000
# Server-side backstop matching EMBEDDING_DOCUMENT_BATCH_MAX_CHUNKS -- the real
# cap is enforced by the caller (enqueue_embedding_tasks_sync's batch grouping);
# this just rejects a malformed/oversized request outright. Lives in settings
# (not here) so llm_installation can read the same limit without importing
# documents.
_MAX_BATCH_SIZE = settings.EMBEDDING_MAX_BATCH_SIZE


# Docker polls /readyz every few seconds. A deep provider healthcheck performs
# a real model.encode(), so cache the successful result for the active runtime
# generation instead of continuously competing with real embedding requests.
# The probe lock prevents overlapping initial model loads when a probe takes
# longer than Docker's healthcheck timeout.
_READINESS_CACHE_LOCK = threading.Lock()
_READINESS_PROBE_LOCK = threading.Lock()
_READINESS_CACHE: tuple[str, dict] | None = None


def _get_cached_readiness(runtime_fingerprint: str) -> dict | None:
    with _READINESS_CACHE_LOCK:
        if _READINESS_CACHE is None or _READINESS_CACHE[0] != runtime_fingerprint:
            return None
        return dict(_READINESS_CACHE[1])


def _set_cached_readiness(runtime_fingerprint: str, payload: dict) -> None:
    global _READINESS_CACHE
    with _READINESS_CACHE_LOCK:
        _READINESS_CACHE = (runtime_fingerprint, dict(payload))


def _reset_readiness_cache() -> None:
    """Clear process-local readiness state after tests or an in-process reload."""
    global _READINESS_CACHE
    with _READINESS_CACHE_LOCK:
        _READINESS_CACHE = None


def livez(request):
    return JsonResponse({"status": "ok"})


def readyz(request):
    try:
        runtime = get_active_embedding_runtime()
    except Exception:
        logger.exception("Embedding runtime readiness check failed")
        return JsonResponse({"status": "not_ready"}, status=503)

    runtime_fingerprint = str(runtime.runtime_fingerprint)
    cached = _get_cached_readiness(runtime_fingerprint)
    if cached is not None:
        return JsonResponse(cached)

    if not _READINESS_PROBE_LOCK.acquire(blocking=False):
        return JsonResponse(
            {"status": "not_ready", "reason": "model_initializing"}, status=503
        )

    readiness_lease = None
    try:
        # Another request may have populated the cache just before this request
        # acquired the probe lock.
        cached = _get_cached_readiness(runtime_fingerprint)
        if cached is not None:
            return JsonResponse(cached)

        # The one-time deep probe is a real encode call and therefore shares
        # the same admission limit as /embed/.
        readiness_lease = execution._EMBED_ADMISSION.try_acquire()
        if readiness_lease is None:
            return JsonResponse(
                {"status": "not_ready", "reason": "embedding_busy"}, status=503
            )

        provider = get_embedding_provider(runtime=runtime)
        info = provider.healthcheck()
        if runtime.dimension and info.get("dimension") != runtime.dimension:
            return JsonResponse(
                {
                    "status": "not_ready",
                    "error": (
                        f"loaded model dimension {info.get('dimension')} does not "
                        f"match active runtime dimension {runtime.dimension}"
                    ),
                },
                status=503,
            )

        payload = {
            "status": "ready",
            **info,
            "runtime_fingerprint": runtime_fingerprint,
            "model_revision": provider.spec.model_revision,
        }
        _set_cached_readiness(runtime_fingerprint, payload)
        return JsonResponse(payload)
    except Exception:
        logger.exception("Embedding model readiness probe failed")
        return JsonResponse({"status": "not_ready"}, status=503)
    finally:
        if readiness_lease is not None:
            readiness_lease.release()
        _READINESS_PROBE_LOCK.release()


def _token_is_valid(request) -> bool:
    expected = settings.EMBEDDING_INTERNAL_TOKEN
    if not expected:
        # Fail closed: an unconfigured token must never be treated as "no
        # auth required" for an endpoint that does real model work.
        return False
    return request.headers.get("Authorization", "") == f"Bearer {expected}"


def run_embedding_with_admission(*args, **kwargs):
    """Compatibility facade; inference admission is owned by execution."""
    from .execution import run_embedding_with_admission as run
    return run(*args, **kwargs)


@csrf_exempt
@require_POST
def embed(request):
    if not _token_is_valid(request):
        return JsonResponse({"error": "unauthorized"}, status=401)

    try:
        payload = json.loads(request.body or b"{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return JsonResponse({"error": "Malformed JSON request body."}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({"error": "Expected a JSON object."}, status=400)

    input_type = payload.get("input_type")
    if input_type not in _ALLOWED_INPUT_TYPES:
        return JsonResponse({"error": "'input_type' must be 'query' or 'document'."}, status=400)

    is_batch = "texts" in payload
    if is_batch and "text" in payload:
        return JsonResponse({"error": "Provide either 'text' or 'texts', not both."}, status=400)
    if is_batch and input_type != "document":
        # Query embedding is always latency-sensitive/single -- batching it
        # would make an interactive search wait behind other queries in the
        # same call, which defeats the point of query priority.
        return JsonResponse(
            {"error": "'texts' (batch) is only supported for input_type='document'."}, status=400
        )

    max_length = payload.get("max_length")
    if max_length is not None and not isinstance(max_length, int):
        return JsonResponse({"error": "'max_length' must be an integer."}, status=400)

    if is_batch:
        texts = payload.get("texts")
        if not isinstance(texts, list) or not texts:
            return JsonResponse({"error": "'texts' must be a non-empty array."}, status=400)
        if len(texts) > _MAX_BATCH_SIZE:
            return JsonResponse({"error": f"'texts' exceeds {_MAX_BATCH_SIZE} items."}, status=422)
        for item in texts:
            if not isinstance(item, str) or not item.strip():
                return JsonResponse({"error": "Every item in 'texts' must be non-empty text."}, status=400)
            if len(item) > _MAX_TEXT_CHARS:
                return JsonResponse(
                    {"error": f"An item in 'texts' exceeds {_MAX_TEXT_CHARS} characters."}, status=422
                )
    else:
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            return JsonResponse({"error": "'text' is required."}, status=400)
        if len(text) > _MAX_TEXT_CHARS:
            return JsonResponse({"error": f"'text' exceeds {_MAX_TEXT_CHARS} characters."}, status=422)

    from .routing import runtime_identity
    runtime = get_active_embedding_runtime()
    execution_identity = runtime_identity(runtime)
    expected_identity = payload.get("expected_identity")
    if expected_identity is not None and expected_identity != execution_identity:
        return JsonResponse({"error": {"code": "EMBEDDING_IDENTITY_MISMATCH",
                                      "message": "Embedding runtime changed."}}, status=409)

    # model_name/backend are deliberately not accepted from the caller -- this
    # endpoint uses its own active runtime. expected_identity pins the caller's
    # selection and rejects a deployment transition before inference.
    try:
        # A batch is admitted as ONE "document" job -- a single lease covers
        # the whole model.encode() call no matter how many texts it holds.
        results = run_embedding_with_admission(
            input_type,
            text=None if is_batch else text,
            texts=texts if is_batch else None,
            max_length=max_length,
        )
    except EmbeddingBusyError as exc:
        queue_state = execution._EMBED_ADMISSION.snapshot()
        logger.warning(
            "Embedding admission rejected: input_type=%s reason=%s state=%s",
            input_type,
            str(exc),
            queue_state,
        )
        response = JsonResponse(
            {
                "ok": False,
                "error": {
                    "code": "EMBEDDING_BUSY",
                    "message": "Embedding service is busy.",
                    "details": {"reason": str(exc).rsplit("(", 1)[-1].rstrip(")")},
                },
            },
            status=503,
        )
        response["Retry-After"] = os.getenv("EMBEDDING_BUSY_RETRY_AFTER_SECONDS", "5")
        return response

    except Exception:
        logger.exception("Internal %s embedding failed", input_type)
        return JsonResponse({"error": "embedding failed"}, status=500)

    if runtime_identity(get_active_embedding_runtime()) != execution_identity:
        return JsonResponse({"error": "embedding runtime changed during execution"}, status=409)
    for result in results:
        if runtime.dimension and len(result.dense_vector) != runtime.dimension:
            logger.error(
                "Embedding dimension mismatch: got=%s expected=%s runtime=%s",
                len(result.dense_vector),
                runtime.dimension,
                runtime.runtime_fingerprint,
            )
            return JsonResponse({"error": "embedding dimension mismatch"}, status=500)

    if is_batch:
        return JsonResponse(
            {
                "results": [
                    {
                        "dense_vector": result.dense_vector,
                        "sparse_vector": result.sparse_vector,
                        "model_name": result.model_name,
                        "backend": result.backend,
                        "dimension": result.dimension,
                    }
                    for result in results
                ],
                "runtime_fingerprint": runtime.runtime_fingerprint,
                "execution_identity": execution_identity,
            }
        )

    result = results[0]
    return JsonResponse(
        {
            "dense_vector": result.dense_vector,
            "sparse_vector": result.sparse_vector,
            "model_name": result.model_name,
            "backend": result.backend,
            "dimension": result.dimension,
            "runtime_fingerprint": runtime.runtime_fingerprint,
            "execution_identity": execution_identity,
        }
    )


@require_GET
def execution_observation(request):
    if not _token_is_valid(request):
        return JsonResponse({"error": "unauthorized"}, status=401)
    if os.getenv("DOTORI_EMBEDDING_MODEL_PROCESS") != "1":
        return JsonResponse({"error": "not a model-owning process"}, status=503)
    from .execution_observation import observe_embedding_execution
    from .routing import runtime_identity
    runtime = get_active_embedding_runtime()
    readiness = readyz(request)
    if readiness.status_code != 200:
        return JsonResponse({"error": "execution observation unavailable"}, status=503)
    provider = get_embedding_provider(runtime=runtime)
    observed = observe_embedding_execution(runtime, provider)
    if observed is None or runtime_identity(get_active_embedding_runtime()) != runtime_identity(runtime):
        return JsonResponse({"error": "execution observation unavailable"}, status=503)
    return JsonResponse({"execution_identity": runtime_identity(runtime),
                         "observation": observed.model_dump(mode="json")})

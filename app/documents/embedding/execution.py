"""Process-owned inference admission shared by HTTP and durable document work."""
import logging
import os

from .admission import AdmissionRejected, EmbeddingPriorityAdmission
from .providers.base import EmbeddingBusyError
from . import embeding_models

logger = logging.getLogger(__name__)


def _get_positive_int_env(name: str, default: int) -> int:
    raw_value = os.getenv(name, str(default))
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        logger.warning("Invalid %s=%r, falling back to %s", name, raw_value, default)
        return default
    if value < 1:
        logger.warning("%s must be >= 1, falling back to %s", name, default)
        return default
    return value


def _get_nonnegative_int_env(name: str, default: int) -> int:
    raw_value = os.getenv(name, str(default))
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        logger.warning("Invalid %s=%r, falling back to %s", name, raw_value, default)
        return default
    if value < 0:
        logger.warning("%s must be >= 0, falling back to %s", name, default)
        return default
    return value


def _get_nonnegative_float_env(name: str, default: float) -> float:
    raw_value = os.getenv(name, str(default))
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        logger.warning("Invalid %s=%r, falling back to %s", name, raw_value, default)
        return default
    if value < 0:
        logger.warning("%s must be >= 0, falling back to %s", name, default)
        return default
    return value


# Bounds concurrent model.encode() calls across gunicorn's threads and orders
# waiting work at the single model admission point: query before document,
# FIFO within the same request type.
# Concurrent BGE-M3 encode() calls were stress-tested (dense+sparse output
# verified against sequential ground truth across many concurrent rounds --
# see `manage.py check_embedding_concurrency`) with no race conditions found,
# so this is no longer forced to 1. Gunicorn keeps additional threads for
# queued requests and healthchecks; re-run check_embedding_concurrency on the
# target hardware before raising the model concurrency itself.
_EMBED_CONCURRENCY = _get_positive_int_env("EMBEDDING_MODEL_CONCURRENCY", 2)
_EMBED_QUEUE_CAPACITY = _get_nonnegative_int_env("EMBEDDING_REQUEST_QUEUE_CAPACITY", 4)
_EMBED_QUERY_RESERVE = min(
    _get_nonnegative_int_env("EMBEDDING_QUERY_QUEUE_RESERVE", 1),
    _EMBED_QUEUE_CAPACITY,
)
_EMBED_QUEUE_TIMEOUT = _get_nonnegative_float_env(
    "EMBEDDING_REQUEST_QUEUE_TIMEOUT_SECONDS", 15.0
)
_EMBED_ADMISSION = EmbeddingPriorityAdmission(
    concurrency=_EMBED_CONCURRENCY,
    queue_capacity=_EMBED_QUEUE_CAPACITY,
    query_reserve=_EMBED_QUERY_RESERVE,
)


def run_embedding_with_admission(
    input_type: str,
    *,
    text: str | None = None,
    texts: list[str] | None = None,
    max_length: int | None = None,
    **provider_kwargs,
):
    """Run one model operation through the process-wide priority admission.

    Both the public internal `/embed/` handler and durable document executor
    work call this function. The service preserves the existing singleton
    and prevents a model-process job from bypassing query priority.
    """

    from .inference_context import admitted
    try:
        lease = _EMBED_ADMISSION.acquire(input_type, timeout=_EMBED_QUEUE_TIMEOUT)
    except AdmissionRejected as exc:
        raise EmbeddingBusyError(
            f"embedding service is busy ({exc.reason})",
            retry_after_seconds=float(os.getenv("EMBEDDING_BUSY_RETRY_AFTER_SECONDS", "5")),
        ) from exc
    token = admitted.set(True)
    try:
        if texts is not None:
            return embeding_models.embed_documents(texts, max_length=max_length, **provider_kwargs)
        embed_fn = (
            embeding_models.embed_query
            if input_type == "query"
            else embeding_models.embed_document
        )
        return [embed_fn(text or "", max_length=max_length, **provider_kwargs)]
    finally:
        admitted.reset(token)
        lease.release()


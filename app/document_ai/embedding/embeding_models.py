from __future__ import annotations

from django.conf import settings

from document_ai.parsers.config import (
    get_embedding_backend,
    get_embedding_max_tokens,
    get_embedding_model,
)

from .providers.bgem3 import (
    check_normalized as _check_normalized,
    clear_cuda_cache as _clear_cuda_cache,
    coerce_dense_vector as _coerce_dense_vector,
    coerce_sparse_vector as _coerce_sparse_vector,
    get_bgem3_model as _get_bgem3_model,
    normalize_sparse_vector as _normalize_sparse_vector,
    validate_text as _validate_text,
)
from .providers.base import EmbeddingResult
from .registry import get_embedding_provider
from document_ai.services.embedding_runtime_config import EmbeddingRuntimeSnapshot


def _embed_with_bgem3_hybrid(
    text: str,
    model_name: str,
    max_length: int,
    runtime: EmbeddingRuntimeSnapshot | None = None,
) -> EmbeddingResult:
    provider = get_embedding_provider(
        backend="bgem3_hybrid",
        model_name=model_name,
        runtime=runtime,
    )
    return provider.embed_document(text, max_length=max_length)


def bge_m3_embedder(
    text: str,
    model_name: str | None = None,
    max_length: int | None = None,
    backend: str | None = None,
    runtime: EmbeddingRuntimeSnapshot | None = None,
) -> EmbeddingResult:
    normalized_text = _validate_text(text)
    resolved_backend = backend or (
        runtime.provider if runtime else get_embedding_backend()
    )
    resolved_model = model_name or (
        runtime.model_id if runtime else get_embedding_model()
    )
    resolved_max_length = max_length or get_embedding_max_tokens()

    if resolved_backend == "bgem3_hybrid":
        return _embed_with_bgem3_hybrid(
            text=normalized_text,
            model_name=resolved_model,
            max_length=resolved_max_length,
            runtime=runtime,
        )

    provider = get_embedding_provider(
        backend=resolved_backend,
        model_name=resolved_model,
        runtime=runtime,
    )
    return provider.embed_document(normalized_text, max_length=resolved_max_length)


def embed_document(
    text: str,
    model_name: str | None = None,
    max_length: int | None = None,
    backend: str | None = None,
    runtime: EmbeddingRuntimeSnapshot | None = None,
) -> EmbeddingResult:
    return bge_m3_embedder(
        text=text,
        model_name=model_name,
        max_length=max_length,
        backend=backend,
        runtime=runtime,
    )


def embed_query(
    query: str,
    model_name: str | None = None,
    max_length: int | None = None,
    backend: str | None = None,
    runtime: EmbeddingRuntimeSnapshot | None = None,
) -> EmbeddingResult:
    resolved_max_length = max_length or getattr(settings, "SEARCH_QUERY_EMBEDDING_MAX_TOKENS", None)
    resolved_backend = backend or (
        runtime.provider if runtime else get_embedding_backend()
    )
    resolved_model = model_name or (
        runtime.model_id if runtime else get_embedding_model()
    )

    if resolved_backend == "bgem3_hybrid":
        return bge_m3_embedder(
            text=query,
            model_name=resolved_model,
            max_length=resolved_max_length,
            backend=resolved_backend,
            runtime=runtime,
        )

    provider = get_embedding_provider(
        backend=backend,
        model_name=resolved_model,
        runtime=runtime,
    )
    return provider.embed_query(query, max_length=resolved_max_length)

from __future__ import annotations

from django.conf import settings

from document_ai.parsers.config import get_embedding_backend, get_embedding_max_tokens

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


def _embed_with_bgem3_hybrid(
    text: str,
    model_name: str,
    max_length: int,
) -> EmbeddingResult:
    provider = get_embedding_provider(
        backend="bgem3_hybrid",
        model_name=model_name,
    )
    return provider.embed_document(text, max_length=max_length)


def bge_m3_embedder(
    text: str,
    model_name: str = "BAAI/bge-m3",
    max_length: int | None = None,
    backend: str | None = None,
) -> EmbeddingResult:
    normalized_text = _validate_text(text)
    resolved_backend = backend or get_embedding_backend()
    resolved_max_length = max_length or get_embedding_max_tokens()

    if resolved_backend == "bgem3_hybrid":
        return _embed_with_bgem3_hybrid(
            text=normalized_text,
            model_name=model_name,
            max_length=resolved_max_length,
        )

    provider = get_embedding_provider(
        backend=resolved_backend,
        model_name=model_name,
    )
    return provider.embed_document(normalized_text, max_length=resolved_max_length)


def embed_document(
    text: str,
    model_name: str = "BAAI/bge-m3",
    max_length: int | None = None,
    backend: str | None = None,
) -> EmbeddingResult:
    return bge_m3_embedder(
        text=text,
        model_name=model_name,
        max_length=max_length,
        backend=backend,
    )


def embed_query(
    query: str,
    model_name: str = "BAAI/bge-m3",
    max_length: int | None = None,
    backend: str | None = None,
) -> EmbeddingResult:
    resolved_max_length = max_length or getattr(settings, "QUERY_EMBEDDING_MAX_TOKENS", None)
    resolved_backend = backend or get_embedding_backend()

    if resolved_backend == "bgem3_hybrid":
        return bge_m3_embedder(
            text=query,
            model_name=model_name,
            max_length=resolved_max_length,
            backend=resolved_backend,
        )

    provider = get_embedding_provider(
        backend=backend,
        model_name=model_name,
    )
    return provider.embed_query(query, max_length=resolved_max_length)

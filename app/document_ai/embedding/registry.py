from __future__ import annotations

import os

from .providers import (
    BGEM3HybridProvider,
    EmbeddingProvider,
    OpenAIEmbeddingProvider,
    SentenceTransformersEmbeddingProvider,
)
from .providers.remote import RemoteEmbeddingProxyProvider
from document_ai.services.embedding_runtime_config import (
    EmbeddingRuntimeSnapshot,
    get_active_embedding_runtime,
)

_PROVIDER_FACTORIES = {
    BGEM3HybridProvider.backend: BGEM3HybridProvider,
    SentenceTransformersEmbeddingProvider.backend: SentenceTransformersEmbeddingProvider,
    OpenAIEmbeddingProvider.backend: OpenAIEmbeddingProvider,
}


def _is_embedding_model_process() -> bool:
    # Only the gunicorn worker dotori-document boots as its model owner sets
    # this (see docker-compose.yml). dotori-document's own Celery worker does
    # NOT set it -- it proxies through the same /embed endpoint as app, so the
    # model is loaded in exactly one OS process, not one per process.
    return os.getenv("DOTORI_EMBEDDING_MODEL_PROCESS") == "1"


def get_embedding_provider(
    *,
    backend: str | None = None,
    model_name: str | None = None,
    dimension: int | None = None,
    runtime: EmbeddingRuntimeSnapshot | None = None,
) -> EmbeddingProvider:
    resolved_runtime = runtime or get_active_embedding_runtime()
    resolved_backend = backend or resolved_runtime.provider
    resolved_model = model_name or resolved_runtime.model_id
    resolved_dimension = (
        dimension if dimension is not None else resolved_runtime.dimension
    )

    try:
        factory = _PROVIDER_FACTORIES[resolved_backend]
    except KeyError as exc:
        raise ValueError(f"Unsupported embedding backend: {resolved_backend}") from exc

    # Local in-process models must load in exactly one process (dotori-document's
    # model-owning gunicorn worker). Every other process (app, Celery workers)
    # gets a proxy that calls out to it over HTTP.
    # External API providers (e.g. openai_compatible) make outbound HTTP calls directly.
    local_backends = (
        BGEM3HybridProvider.backend,
        SentenceTransformersEmbeddingProvider.backend,
    )
    model_revision = (
        getattr(resolved_runtime, "model_revision", "")
        if resolved_model == getattr(resolved_runtime, "model_id", None)
        else ""
    )
    normalize_embeddings = getattr(resolved_runtime, "normalize_embeddings", True)
    query_prefix = getattr(resolved_runtime, "query_prefix", "")
    document_prefix = getattr(resolved_runtime, "document_prefix", "")

    if resolved_backend in local_backends and not _is_embedding_model_process():
        supports_sparse = getattr(
            resolved_runtime,
            "supports_sparse",
            resolved_backend == BGEM3HybridProvider.backend,
        )
        default_distance = getattr(resolved_runtime, "distance_strategy", "cosine")
        return RemoteEmbeddingProxyProvider(
            backend=resolved_backend,
            model_name=resolved_model,
            model_revision=model_revision,
            dimension=resolved_dimension,
            supports_sparse=supports_sparse,
            normalize_embeddings=normalize_embeddings,
            query_prefix=query_prefix,
            document_prefix=document_prefix,
            default_distance=default_distance,
        )

    return factory(
        model_name=resolved_model,
        model_revision=model_revision,
        dimension=resolved_dimension,
        normalize_embeddings=normalize_embeddings,
        query_prefix=query_prefix,
        document_prefix=document_prefix,
    )


def registered_embedding_backends() -> list[str]:
    return sorted(_PROVIDER_FACTORIES)

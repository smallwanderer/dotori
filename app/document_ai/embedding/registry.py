from __future__ import annotations

from .providers import BGEM3HybridProvider, EmbeddingProvider
from document_ai.services.embedding_runtime_config import (
    EmbeddingRuntimeSnapshot,
    get_active_embedding_runtime,
)

_PROVIDER_FACTORIES = {
    BGEM3HybridProvider.backend: BGEM3HybridProvider,
}


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

    return factory(
        model_name=resolved_model,
        model_revision=(
            resolved_runtime.model_revision
            if resolved_model == resolved_runtime.model_id
            else ""
        ),
        dimension=resolved_dimension,
        normalize_embeddings=resolved_runtime.normalize_embeddings,
        query_prefix=resolved_runtime.query_prefix,
        document_prefix=resolved_runtime.document_prefix,
    )


def registered_embedding_backends() -> list[str]:
    return sorted(_PROVIDER_FACTORIES)

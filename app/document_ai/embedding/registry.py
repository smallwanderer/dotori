from __future__ import annotations

from document_ai.parsers.config import (
    get_embedding_backend,
    get_embedding_dimension,
    get_embedding_model,
)

from .providers import BGEM3HybridProvider, EmbeddingProvider

_PROVIDER_FACTORIES = {
    BGEM3HybridProvider.backend: BGEM3HybridProvider,
}


def get_embedding_provider(
    *,
    backend: str | None = None,
    model_name: str | None = None,
    dimension: int | None = None,
) -> EmbeddingProvider:
    resolved_backend = backend or get_embedding_backend()
    resolved_model = model_name or get_embedding_model()
    resolved_dimension = dimension if dimension is not None else get_embedding_dimension()

    try:
        factory = _PROVIDER_FACTORIES[resolved_backend]
    except KeyError as exc:
        raise ValueError(f"Unsupported embedding backend: {resolved_backend}") from exc

    return factory(
        model_name=resolved_model,
        dimension=resolved_dimension,
    )


def registered_embedding_backends() -> list[str]:
    return sorted(_PROVIDER_FACTORIES)

from __future__ import annotations

from document_ai.parsers.config import (
    get_embedding_backend,
    get_embedding_dimension,
    get_embedding_model,
    get_embedding_sparse_enabled,
    get_embedding_store,
)

from .stores import EmbeddingStore, PgVectorChunkEmbeddingStore

_STORE_FACTORIES = {
    PgVectorChunkEmbeddingStore.name: PgVectorChunkEmbeddingStore,
}


def get_embedding_store_instance(
    *,
    store_name: str | None = None,
    model_name: str | None = None,
    backend: str | None = None,
    dimension: int | None = None,
    supports_sparse: bool | None = None,
    distance_strategy: str | None = None,
) -> EmbeddingStore:
    """
    This function instantiates the appropriate embedding store based on configuration.
    It reads the following configuration values (in order of precedence):

    1. Configured via arguments (store_name, model_name, backend, etc.)
    2. Environment variables (EMBEDDING_STORE, EMBEDDING_MODEL, etc.)
    3. Hardcoded defaults (in module-level defaults and internal logic)

    The default store is defined in settings.py via EMBEDDING_STORE_DEFAULT,
    with PgVectorChunkEmbeddingStore as the fallback used when EMBEDDING_STORE is not set.
    """
    resolved_store_name = store_name or get_embedding_store()
    resolved_model_name = model_name or get_embedding_model()
    resolved_backend = backend or get_embedding_backend()
    resolved_dimension = dimension if dimension is not None else get_embedding_dimension()
    resolved_supports_sparse = (
        supports_sparse if supports_sparse is not None else get_embedding_sparse_enabled()
    )

    if resolved_dimension is None:
        raise ValueError("EMBEDDING_DIMENSION is required for embedding store selection.")

    try:
        factory = _STORE_FACTORIES[resolved_store_name]
    except KeyError as exc:
        raise ValueError(f"Unsupported embedding store: {resolved_store_name}") from exc

    return factory(
        model_name=resolved_model_name,
        backend=resolved_backend,
        dimension=resolved_dimension,
        supports_sparse=resolved_supports_sparse,
        distance_strategy=distance_strategy,
    )


def registered_embedding_stores() -> list[str]:
    return sorted(_STORE_FACTORIES)

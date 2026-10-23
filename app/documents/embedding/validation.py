from __future__ import annotations

from django.db import connection

from document_ai.parsers.config import (
    get_embedding_backend,
    get_embedding_dimension,
    get_embedding_model,
    get_embedding_sparse_enabled,
    get_embedding_store,
)

from .registry import get_embedding_provider
from .store_registry import get_embedding_store_instance
from document_ai.services.embedding_runtime_config import get_active_embedding_runtime


def get_active_embedding_config() -> dict:
    runtime = get_active_embedding_runtime()
    provider = get_embedding_provider()
    store = get_embedding_store_instance()
    return {
        "backend": get_embedding_backend(),
        "model_name": get_embedding_model(),
        "dimension": get_embedding_dimension(),
        "store": get_embedding_store(),
        "store_dimension": store.spec.dimension,
        "store_dense_field": store.spec.dense_field,
        "supports_sparse": provider.spec.supports_sparse,
        "sparse_enabled": get_embedding_sparse_enabled(),
        "default_distance": provider.spec.default_distance,
        "generation_id": runtime.generation_id,
        "model_revision": runtime.model_revision,
        "runtime_fingerprint": runtime.runtime_fingerprint,
    }


def get_pgvector_column_dimension(table_name: str, column_name: str) -> int | None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT atttypmod
            FROM pg_attribute
            WHERE attrelid = %s::regclass
              AND attname = %s
              AND NOT attisdropped
            """,
            [table_name, column_name],
        )
        row = cursor.fetchone()

    if not row:
        return None

    typmod = row[0]
    if typmod is None or typmod < 0:
        return None

    # pgvector stores vector dimension as typmod - 4.
    return int(typmod) - 4


def validate_active_embedding_provider(*, validate_db_schema: bool = True) -> dict:
    runtime = get_active_embedding_runtime()
    provider = get_embedding_provider()
    store = get_embedding_store_instance()
    result = provider.embed_query("embedding healthcheck", max_length=32)
    store.validate_embedding(result)

    expected_dimension = get_embedding_dimension()
    actual_dimension = len(result.dense_vector or [])

    if expected_dimension and actual_dimension != expected_dimension:
        raise RuntimeError(
            f"Embedding dimension mismatch: expected={expected_dimension}, actual={actual_dimension}"
        )

    if get_embedding_sparse_enabled() and not result.sparse_vector:
        raise RuntimeError("Sparse embedding is enabled but provider returned no sparse vector.")

    db_dimension = None
    if validate_db_schema:
        db_dimension = get_pgvector_column_dimension(
            "document_ai_chunkembedding",
            store.spec.dense_field,
        )
        if db_dimension and expected_dimension and db_dimension != expected_dimension:
            raise RuntimeError(
                f"DB vector dimension mismatch: db={db_dimension}, configured={expected_dimension}"
            )
        if db_dimension and db_dimension != actual_dimension:
            raise RuntimeError(
                f"DB vector dimension mismatch: db={db_dimension}, actual={actual_dimension}"
            )

    return {
        "backend": result.backend,
        "model_name": result.model_name,
        "store": store.spec.name,
        "dimension": actual_dimension,
        "configured_dimension": expected_dimension,
        "store_dimension": store.spec.dimension,
        "db_dimension": db_dimension,
        "supports_sparse": bool(result.sparse_vector),
        "generation_id": runtime.generation_id,
        "model_revision": runtime.model_revision,
        "runtime_fingerprint": runtime.runtime_fingerprint,
    }

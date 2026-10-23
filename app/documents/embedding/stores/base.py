from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from document_ai.embedding.providers.base import EmbeddingResult


@dataclass(frozen=True)
class EmbeddingStoreSpec:
    name: str
    dimension: int
    dense_field: str
    sparse_field: str | None
    supports_sparse: bool
    model_name: str
    backend: str
    generation_id: str
    model_revision: str
    distance_strategy: str = "inner_product"


class EmbeddingStore(Protocol):
    spec: EmbeddingStoreSpec

    def validate_embedding(self, embedding: EmbeddingResult) -> None:
        ...

    def save_chunk_embedding(self, *, chunk, embedding: EmbeddingResult, status: str) -> None:
        ...

    def mark_chunk_embedding_failed(self, *, chunk, error_message: str, status: str) -> None:
        ...

    def completed_embedding_exists(self, *, chunk_id_ref):
        ...

    def completed_chunk_ids(self):
        ...

    def base_queryset(self):
        ...

    def distance_annotation(self, query_vector):
        ...

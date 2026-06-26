from __future__ import annotations

from django.conf import settings
from django.utils import timezone
from pgvector.django import CosineDistance, L2Distance, MaxInnerProduct

from config.enums import AIStatus
from document_ai.embedding.providers.base import EmbeddingResult
from document_ai.models import ChunkEmbedding

from .base import EmbeddingStoreSpec


class PgVectorChunkEmbeddingStore:
    name = "pgvector_chunk_1024"

    def __init__(
        self,
        *,
        model_name: str,
        backend: str,
        dimension: int,
        supports_sparse: bool,
        distance_strategy: str | None = None,
    ):
        self.spec = EmbeddingStoreSpec(
            name=self.name,
            dimension=dimension,
            dense_field="vector",
            sparse_field="sparse_vector",
            supports_sparse=supports_sparse,
            model_name=model_name,
            backend=backend,
            distance_strategy=distance_strategy
            or getattr(settings, "EMBEDDING_DISTANCE_STRATEGY", "inner_product"),
        )

    def validate_embedding(self, embedding: EmbeddingResult) -> None:
        actual_dimension = len(embedding.dense_vector or [])
        if actual_dimension != self.spec.dimension:
            raise ValueError(
                "Embedding dimension does not match active store: "
                f"store={self.spec.name}, expected={self.spec.dimension}, actual={actual_dimension}"
            )
        if self.spec.model_name != embedding.model_name:
            raise ValueError(
                "Embedding model does not match active store: "
                f"store={self.spec.model_name}, embedding={embedding.model_name}"
            )
        if self.spec.backend != embedding.backend:
            raise ValueError(
                "Embedding backend does not match active store: "
                f"store={self.spec.backend}, embedding={embedding.backend}"
            )
        if self.spec.supports_sparse and not embedding.sparse_vector:
            raise ValueError("Active embedding store expects sparse vectors, but embedding returned none.")

    def save_chunk_embedding(self, *, chunk, embedding: EmbeddingResult, status: str = AIStatus.COMPLETED) -> None:
        self.validate_embedding(embedding)
        ChunkEmbedding.objects.update_or_create(
            chunk=chunk,
            model_name=self.spec.model_name,
            model_version=self.spec.backend,
            defaults={
                self.spec.dense_field: embedding.dense_vector,
                self.spec.sparse_field or "sparse_vector": embedding.sparse_vector or {},
                "embedded_at": timezone.now(),
                "status": status,
                "error_message": "",
            },
        )

    def mark_chunk_embedding_failed(self, *, chunk, error_message: str, status: str = AIStatus.FAILED) -> None:
        ChunkEmbedding.objects.update_or_create(
            chunk=chunk,
            model_name=self.spec.model_name,
            model_version=self.spec.backend,
            defaults={
                self.spec.dense_field: None,
                self.spec.sparse_field or "sparse_vector": {},
                "embedded_at": None,
                "status": status,
                "error_message": error_message,
            },
        )

    def completed_embedding_exists(self, *, chunk_id_ref):
        return ChunkEmbedding.objects.filter(
            chunk_id=chunk_id_ref,
            model_name=self.spec.model_name,
            model_version=self.spec.backend,
            status=AIStatus.COMPLETED,
        )

    def completed_chunk_ids(self):
        return ChunkEmbedding.objects.filter(
            model_name=self.spec.model_name,
            model_version=self.spec.backend,
            status=AIStatus.COMPLETED,
        ).values_list("chunk_id", flat=True)

    def base_queryset(self):
        return (
            ChunkEmbedding.objects.select_related(
                "chunk",
                "chunk__parse_result",
                "chunk__parse_result__node",
            )
            .filter(
                model_name=self.spec.model_name,
                model_version=self.spec.backend,
                status=AIStatus.COMPLETED,
                chunk__parse_result__node__trashed=False,
            )
        )

    def distance_annotation(self, query_vector):
        if self.spec.distance_strategy == "cosine":
            return CosineDistance(self.spec.dense_field, query_vector)
        if self.spec.distance_strategy == "l2":
            return L2Distance(self.spec.dense_field, query_vector)
        if self.spec.distance_strategy == "inner_product":
            return MaxInnerProduct(self.spec.dense_field, query_vector)
        raise ValueError(f"Unknown distance strategy: {self.spec.distance_strategy}")

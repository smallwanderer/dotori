from django.db import models
from pgvector.django import HnswIndex, VectorField

from config.enums import AIStatus


class EmbeddingGeneration(models.Model):
    generation_id = models.CharField(max_length=96, unique=True)
    scope = models.CharField(max_length=32, default="production")
    runtime_fingerprint = models.CharField(max_length=64, blank=True)
    catalog_id = models.CharField(max_length=96, blank=True)
    model_id = models.CharField(max_length=128)
    model_revision = models.CharField(max_length=64, blank=True)
    provider = models.CharField(max_length=64)
    store = models.CharField(max_length=64)
    dimension = models.PositiveIntegerField(default=1024)
    supports_sparse = models.BooleanField(default=True)
    status = models.CharField(max_length=32, default="READY")
    expected_chunks = models.PositiveIntegerField(default=0)
    completed_chunks = models.PositiveIntegerField(default=0)
    failed_chunks = models.PositiveIntegerField(default=0)
    activated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["scope", "status"]),
        ]

    def __str__(self):
        return f"{self.scope}:{self.generation_id} ({self.status})"


class ChunkEmbedding(models.Model):
    """
    bge-m3 embedding 결과 저장 모델
    - 어떤 chunk를 어떤 모델로 벡터화했는지 저장
    """

    chunk = models.OneToOneField(
        "document_ai.DocumentChunk",
        on_delete=models.CASCADE,
        related_name="embedding",
    )

    # Multi-dimension vector fields for different embedding models
    # BGE-M3 (1024), Harrier (640), Granite-278m (768), Qwen/OpenAI (1536), Granite-107m/MiniLM (384)
    vector = VectorField(dimensions=1024, null=True, blank=True)
    vector_640 = VectorField(dimensions=640, null=True, blank=True)
    vector_768 = VectorField(dimensions=768, null=True, blank=True)
    vector_1536 = VectorField(dimensions=1536, null=True, blank=True)
    vector_384 = VectorField(dimensions=384, null=True, blank=True)
    sparse_vector = models.JSONField(default=dict, blank=True)

    # Status
    status = models.CharField(
        max_length=32,
        choices=AIStatus.choices,
        default=AIStatus.PENDING,
    )

    # Error
    error_message = models.CharField(max_length=255, blank=True)

    embedded_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


    class Meta:
        indexes = [
            HnswIndex(
                name='chunk_embedding_active_hnsw_idx',
                fields=['vector'],
                m=16,
                ef_construction=64,
                opclasses=['vector_ip_ops']
            ),
        ]

    def __str__(self):
        return f"{self.chunk.parse_result.node.name} - embedding"

    def to_dict(self):
        return {
            "id": self.id,
            "chunk_id": self.chunk_id,
            "sparse_terms": len(self.sparse_vector or {}),
            "embedded_at": self.embedded_at.isoformat() if self.embedded_at else None,
            "created_at": self.created_at.isoformat(),
        }


class ChunkSegmentEmbedding(models.Model):
    """
    검색/RAG contextual compression에서 lazy 생성하는 segment embedding.
    chunk embedding과 동일한 임베딩 정책을 사용하므로 별도 모델 식별자는 저장하지 않는다.
    """

    chunk = models.ForeignKey(
        "document_ai.DocumentChunk",
        on_delete=models.CASCADE,
        related_name="segment_embeddings",
        db_index=False,
    )
    window_size = models.PositiveSmallIntegerField(default=2)
    segment_index = models.PositiveIntegerField()
    text = models.TextField()
    char_start = models.PositiveIntegerField(default=0)
    char_end = models.PositiveIntegerField(default=0)
    vector = VectorField(dimensions=1024, null=True, blank=True)
    sparse_vector = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["chunk", "window_size", "segment_index"]
        constraints = [
            models.UniqueConstraint(
                fields=["chunk", "window_size", "segment_index"],
                name="uniq_segment_emb_per_chunk_window_index",
            )
        ]
        indexes = [
            models.Index(fields=["last_used_at"]),
        ]

    def __str__(self):
        return f"{self.chunk_id} - Segment {self.segment_index} (w={self.window_size})"

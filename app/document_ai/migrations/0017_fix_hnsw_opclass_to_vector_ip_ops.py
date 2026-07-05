"""
Migration: Fix HNSW opclass mismatch

변경 전: vector_cosine_ops  (cosine distance 전용 인덱스)
변경 후: vector_ip_ops       (inner product 전용 인덱스)

런타임 거리 전략(inner_product / MaxInnerProduct)과 HNSW opclass가 일치해야
pgvector가 seq scan 대신 HNSW 인덱스를 사용합니다.
"""

from django.db import migrations
import pgvector.django.indexes


class Migration(migrations.Migration):

    dependencies = [
        ("document_ai", "0016_ragjob_answer_mode_ragjob_query_confidence_and_more"),
    ]

    operations = [
        # ── ChunkEmbedding ──────────────────────────────────────────────
        migrations.RemoveIndex(
            model_name="chunkembedding",
            name="chunk_embedding_vector_hnsw_idx",
        ),
        migrations.AddIndex(
            model_name="chunkembedding",
            index=pgvector.django.indexes.HnswIndex(
                name="chunk_embedding_vector_hnsw_idx",
                fields=["vector"],
                m=16,
                ef_construction=64,
                opclasses=["vector_ip_ops"],
            ),
        ),
        # ── ChunkSentenceEmbedding ──────────────────────────────────────
        migrations.RemoveIndex(
            model_name="chunksentenceembedding",
            name="sent_emb_vec_hnsw_idx",
        ),
        migrations.AddIndex(
            model_name="chunksentenceembedding",
            index=pgvector.django.indexes.HnswIndex(
                name="sent_emb_vec_hnsw_idx",
                fields=["vector"],
                m=16,
                ef_construction=64,
                opclasses=["vector_ip_ops"],
            ),
        ),
    ]

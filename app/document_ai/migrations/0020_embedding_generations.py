from django.db import migrations, models
import django.db.models.deletion


LEGACY_GENERATION_ID = "legacy-bge-m3"


def create_legacy_generation(apps, schema_editor):
    EmbeddingGeneration = apps.get_model("document_ai", "EmbeddingGeneration")
    EmbeddingGeneration.objects.get_or_create(
        generation_id=LEGACY_GENERATION_ID,
        defaults={
            "scope": "production",
            "catalog_id": "legacy-env",
            "model_id": "BAAI/bge-m3",
            "model_revision": "legacy",
            "provider": "bgem3_hybrid",
            "store": "pgvector_chunk_1024",
            "dimension": 1024,
            "supports_sparse": True,
            "status": "ACTIVE",
        },
    )


class Migration(migrations.Migration):
    dependencies = [
        ("document_ai", "0019_alter_queryunderstandinglog_intent_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="EmbeddingGeneration",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("generation_id", models.CharField(max_length=96, unique=True)),
                ("scope", models.CharField(db_index=True, default="production", max_length=32)),
                ("runtime_fingerprint", models.CharField(blank=True, db_index=True, max_length=64)),
                ("catalog_id", models.CharField(blank=True, max_length=96)),
                ("model_id", models.CharField(max_length=128)),
                ("model_revision", models.CharField(blank=True, max_length=64)),
                ("provider", models.CharField(max_length=64)),
                ("store", models.CharField(max_length=64)),
                ("dimension", models.PositiveIntegerField(default=1024)),
                ("supports_sparse", models.BooleanField(default=True)),
                ("status", models.CharField(db_index=True, default="READY", max_length=32)),
                ("expected_chunks", models.PositiveIntegerField(default=0)),
                ("completed_chunks", models.PositiveIntegerField(default=0)),
                ("failed_chunks", models.PositiveIntegerField(default=0)),
                ("activated_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "indexes": [
                    models.Index(fields=["scope", "status"], name="document_ai_scope_1d5cc1_idx"),
                    models.Index(fields=["model_id", "model_revision"], name="document_ai_model_i_ba50c4_idx"),
                ],
            },
        ),
        migrations.RunPython(create_legacy_generation, migrations.RunPython.noop),
        migrations.AddField(
            model_name="chunkembedding",
            name="generation",
            field=models.ForeignKey(
                db_column="generation_id",
                default=LEGACY_GENERATION_ID,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="chunk_embeddings",
                to="document_ai.embeddinggeneration",
                to_field="generation_id",
            ),
            preserve_default=True,
        ),
        migrations.AddField(
            model_name="chunkembedding",
            name="model_revision",
            field=models.CharField(default="legacy", max_length=64),
        ),
        migrations.AddField(
            model_name="chunkembedding",
            name="provider",
            field=models.CharField(default="bgem3_hybrid", max_length=64),
        ),
        migrations.AddField(
            model_name="chunksentenceembedding",
            name="generation",
            field=models.ForeignKey(
                db_column="generation_id",
                default=LEGACY_GENERATION_ID,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="sentence_embeddings",
                to="document_ai.embeddinggeneration",
                to_field="generation_id",
            ),
            preserve_default=True,
        ),
        migrations.AddField(
            model_name="chunksegmentembedding",
            name="generation",
            field=models.ForeignKey(
                db_column="generation_id",
                default=LEGACY_GENERATION_ID,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="segment_embeddings",
                to="document_ai.embeddinggeneration",
                to_field="generation_id",
            ),
            preserve_default=True,
        ),
        migrations.RemoveConstraint(
            model_name="chunkembedding",
            name="uniq_embedding_per_chunk_model_version",
        ),
        migrations.RemoveConstraint(
            model_name="chunksentenceembedding",
            name="uniq_sentence_emb_per_chunk_index",
        ),
        migrations.RemoveConstraint(
            model_name="chunksegmentembedding",
            name="uniq_segment_emb_per_chunk_window_index",
        ),
        migrations.AddConstraint(
            model_name="chunkembedding",
            constraint=models.UniqueConstraint(
                fields=("chunk", "generation"),
                name="uniq_embedding_per_chunk_generation",
            ),
        ),
        migrations.AddConstraint(
            model_name="chunksentenceembedding",
            constraint=models.UniqueConstraint(
                fields=("chunk", "generation", "sentence_index"),
                name="uniq_sentence_emb_per_chunk_generation_index",
            ),
        ),
        migrations.AddConstraint(
            model_name="chunksegmentembedding",
            constraint=models.UniqueConstraint(
                fields=("chunk", "generation", "window_size", "segment_index"),
                name="uniq_segment_emb_per_chunk_generation_window_index",
            ),
        ),
        migrations.AddIndex(
            model_name="chunkembedding",
            index=models.Index(fields=["generation", "status"], name="document_ai_generat_0668eb_idx"),
        ),
    ]

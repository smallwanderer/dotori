import uuid

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("documents", "0006_parse_revision"),
        ("orchestration_state", "0003_plan_command_delivery"),
    ]
    operations = [migrations.CreateModel(
        name="DocumentEmbeddingManifest",
        fields=[
            ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
            ("step_id", models.CharField(max_length=64)),
            ("job_id", models.CharField(max_length=64, unique=True)),
            ("command", models.JSONField()),
            ("targets", models.JSONField()),
            ("target_count", models.PositiveIntegerField()),
            ("fingerprint", models.CharField(max_length=64)),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("node", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                related_name="embedding_manifests", to="files.node")),
            ("plan", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                related_name="document_embedding_manifests", to="orchestration_state.plansubmission")),
        ],
        options={"constraints": [models.UniqueConstraint(fields=("plan", "step_id"),
            name="docs_embed_manifest_step_uniq")]},
    )]

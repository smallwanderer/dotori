"""Immutable approved embedding input; neither an attempt nor a receipt."""
import uuid

from django.db import models


class DocumentEmbeddingManifest(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    plan = models.ForeignKey("orchestration_state.PlanSubmission", on_delete=models.CASCADE,
                             related_name="document_embedding_manifests")
    node = models.ForeignKey("files.Node", on_delete=models.CASCADE,
                             related_name="embedding_manifests")
    step_id = models.CharField(max_length=64)
    job_id = models.CharField(max_length=64, unique=True)
    command = models.JSONField()
    targets = models.JSONField()
    target_count = models.PositiveIntegerField()
    fingerprint = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["plan", "step_id"],
                                               name="docs_embed_manifest_step_uniq")]

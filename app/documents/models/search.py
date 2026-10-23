from django.db import models
from django.conf import settings

from config.enums import AIStatus, QueryAnswerMode, QueryIntent

from ._common import _ensure_workspace_from_owner


class QueryUnderstandingLog(models.Model):
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="query_understanding_logs",
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="documents_query_understanding_logs",
    )
    mode = models.CharField(max_length=32, default="search", db_index=True)
    raw_query = models.TextField()
    normalized_query = models.TextField(blank=True)
    semantic_query = models.TextField(blank=True)
    intent = models.CharField(
        max_length=64,
        choices=QueryIntent.choices,
        default=QueryIntent.AMBIGUOUS,
        db_index=True,
    )
    answer_mode = models.CharField(
        max_length=64,
        choices=QueryAnswerMode.choices,
        default=QueryAnswerMode.AMBIGUOUS,
    )
    retrieval_required = models.BooleanField(default=True)
    confidence = models.FloatField(default=0.0)
    reason = models.CharField(max_length=255, blank=True)
    source = models.CharField(max_length=64, blank=True, db_index=True)
    status = models.CharField(max_length=32, default="success", db_index=True)
    warnings = models.JSONField(default=list, blank=True)
    classification = models.JSONField(default=dict, blank=True)
    query_dsl = models.JSONField(default=dict, blank=True)
    orm = models.JSONField(default=dict, blank=True)
    raw_result = models.JSONField(default=dict, blank=True)
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["workspace", "mode", "-created_at"], name="docai_qu_ws_mode_created_idx"),
            models.Index(fields=["owner", "mode", "-created_at"], name="docs_qu_owner_mode_created_idx"),
            models.Index(fields=["intent", "-created_at"], name="docs_qu_intent_created_idx"),
            models.Index(fields=["retrieval_required", "-created_at"], name="docs_qu_retrieval_created_idx"),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"QueryUnderstandingLog({self.id}) {self.intent}: {self.raw_query[:40]}"

    def save(self, *args, **kwargs):
        _ensure_workspace_from_owner(self)
        return super().save(*args, **kwargs)


class SearchJob(models.Model):
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="search_jobs",
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="documents_search_jobs",
    )
    query_log = models.ForeignKey(
        "documents.QueryUnderstandingLog",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="search_jobs",
    )

    query = models.TextField()
    top_k = models.PositiveIntegerField(default=5)
    threshold = models.FloatField(null=True, blank=True)
    node_ids = models.JSONField(default=list, blank=True)
    tuning_params = models.JSONField(default=dict, blank=True)

    status = models.CharField(
        max_length=32,
        choices=AIStatus.choices,
        default=AIStatus.PENDING,
        db_index=True,
    )
    task_id = models.CharField(max_length=255, blank=True)
    results = models.JSONField(default=list, blank=True)
    error_message = models.TextField(blank=True)
    performance_metrics = models.JSONField(default=dict, blank=True)

    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["workspace", "status", "-created_at"], name="docai_se_ws_status_created_idx"),
            models.Index(fields=["owner", "status", "-created_at"], name="docs_sj_own_st_created_idx"),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"SearchJob({self.id}) {self.status}: {self.query[:40]}"

    def save(self, *args, **kwargs):
        _ensure_workspace_from_owner(self)
        if self.query_log_id and self.query_log.workspace_id != self.workspace_id:
            raise ValueError("Search job and query log must belong to the same workspace.")
        return super().save(*args, **kwargs)

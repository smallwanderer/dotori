import uuid

from django.conf import settings
from django.db import models


class WorkspaceQualityProfileRevision(models.Model):
    STATUS_DRAFT = "draft"
    STATUS_ACTIVE = "active"
    STATUS_ARCHIVED = "archived"
    STATUS_CHOICES = [
        (STATUS_DRAFT, "Draft"),
        (STATUS_ACTIVE, "Active"),
        (STATUS_ARCHIVED, "Archived"),
    ]

    AXIS_RETRIEVAL = "retrieval"
    AXIS_GENERATION = "generation"
    AXIS_PROMPT_POLICY = "prompt_policy"
    AXIS_CHOICES = [
        (AXIS_RETRIEVAL, "Retrieval"),
        (AXIS_GENERATION, "Generation"),
        (AXIS_PROMPT_POLICY, "Prompt policy"),
    ]

    uid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, db_index=True)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="quality_profile_revisions",
    )
    version = models.PositiveIntegerField()
    revision = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, db_index=True)
    changed_axes = models.JSONField(default=list, blank=True)
    schema_version = models.PositiveIntegerField(default=1)
    retrieval_config = models.JSONField(default=dict, blank=True)
    generation_config = models.JSONField(default=dict, blank=True)
    prompt_policy = models.JSONField(default=dict, blank=True)
    validation_state = models.CharField(max_length=16, default="not_run")
    validation_warnings = models.JSONField(default=list, blank=True)
    applied_evaluation_run_uid = models.UUIDField(null=True, blank=True)
    based_on = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="derived_revisions",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.SET_NULL,
        related_name="created_quality_profile_revisions",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    applied_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["-version", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "version"],
                name="uniq_quality_profile_workspace_version",
            ),
            models.UniqueConstraint(
                fields=["workspace"],
                condition=models.Q(status="active"),
                name="uniq_active_quality_profile_workspace",
            ),
            models.UniqueConstraint(
                fields=["workspace"],
                condition=models.Q(status="draft"),
                name="uniq_draft_quality_profile_workspace",
            ),
        ]


class WorkspaceEvaluationDataset(models.Model):
    uid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, db_index=True)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="evaluation_datasets",
    )
    axis = models.CharField(max_length=16, choices=WorkspaceQualityProfileRevision.AXIS_CHOICES)
    name = models.CharField(max_length=200)
    items = models.JSONField(default=list, blank=True)
    item_count = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.SET_NULL,
        related_name="created_evaluation_datasets",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["workspace", "axis", "-created_at"], name="eval_dataset_ws_axis_idx"),
        ]


class WorkspaceQualityEvaluationRun(models.Model):
    STATUS_PENDING = "pending"
    STATUS_RUNNING = "running"
    STATUS_SUCCEEDED = "succeeded"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_RUNNING, "Running"),
        (STATUS_SUCCEEDED, "Succeeded"),
        (STATUS_FAILED, "Failed"),
    ]

    uid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, db_index=True)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="quality_evaluation_runs",
    )
    axis = models.CharField(max_length=16, choices=WorkspaceQualityProfileRevision.AXIS_CHOICES)
    dataset = models.ForeignKey(
        WorkspaceEvaluationDataset,
        on_delete=models.PROTECT,
        related_name="evaluation_runs",
    )
    profile_revision = models.ForeignKey(
        WorkspaceQualityProfileRevision,
        on_delete=models.SET_NULL,
        null=True,
        related_name="evaluation_runs",
    )
    tested_revision_number = models.PositiveIntegerField(
        help_text="profile_revision.revision at the moment this run was started; "
        "used to detect a draft edited after evaluation.",
    )
    config_snapshot = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True)
    metrics = models.JSONField(default=dict, blank=True)
    error_message = models.CharField(max_length=500, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.SET_NULL,
        related_name="created_evaluation_runs",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["workspace", "axis", "-created_at"], name="eval_run_ws_axis_idx"),
        ]

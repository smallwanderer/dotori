"""Common plan intake journal; domain artifacts remain with their modules."""
from django.conf import settings
from django.db import models


class PlanSubmission(models.Model):
    plan_id = models.CharField(max_length=64, primary_key=True)
    request_id = models.CharField(max_length=64)
    job_id = models.CharField(max_length=64, unique=True)
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE)
    source_uid = models.CharField(max_length=64)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    definition = models.JSONField()
    reports = models.JSONField(default=dict, blank=True)
    proposals = models.JSONField(default=dict, blank=True)
    control = models.JSONField(default=dict, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    publish_error = models.TextField(default="", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "documents_plansubmission"  # Preserve the existing physical table.
        constraints = [models.UniqueConstraint(fields=["workspace", "request_id"], name="doc_plan_workspace_request_uniq")]


class PlanCommandDelivery(models.Model):
    """Approved step identity and publication journal, not an execution receipt."""
    plan = models.ForeignKey(PlanSubmission, on_delete=models.CASCADE, related_name="commands")
    step_id = models.CharField(max_length=64)
    job_id = models.CharField(max_length=64, unique=True)
    published_at = models.DateTimeField(null=True, blank=True)
    publish_error = models.TextField(default="", blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["plan", "step_id"], name="plan_command_step_unique")]

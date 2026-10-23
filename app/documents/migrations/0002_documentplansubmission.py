from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("documents", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [migrations.CreateModel(
        name="DocumentPlanSubmission",
        fields=[
            ("plan_id", models.CharField(max_length=64, primary_key=True, serialize=False)),
            ("request_id", models.CharField(max_length=64)),
            ("job_id", models.CharField(max_length=64, unique=True)),
            ("definition", models.JSONField()),
            ("reports", models.JSONField(blank=True, default=dict)),
            ("proposals", models.JSONField(blank=True, default=dict)),
            ("published_at", models.DateTimeField(blank=True, null=True)),
            ("publish_error", models.TextField(blank=True, default="")),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("updated_at", models.DateTimeField(auto_now=True)),
            ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
            ("source", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="files.node")),
            ("workspace", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="workspaces.workspace")),
        ],
        options={"constraints": [models.UniqueConstraint(fields=("workspace", "request_id"), name="doc_plan_workspace_request_uniq")]},
    )]

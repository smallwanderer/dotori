from datetime import timedelta

from django.db import migrations, models
from django.utils import timezone


def backfill_active_execution_leases(apps, schema_editor):
    RAGJob = apps.get_model("document_ai", "RAGJob")
    now = timezone.now()
    for job in RAGJob.objects.filter(status__in=["pending", "processing"]):
        anchor = job.updated_at or job.created_at or now
        job.deadline_at = anchor + timedelta(seconds=360)
        job.lease_expires_at = anchor + timedelta(seconds=30)
        job.lease_heartbeat_at = anchor
        job.save(update_fields=["deadline_at", "lease_expires_at", "lease_heartbeat_at"])


class Migration(migrations.Migration):

    dependencies = [("document_ai", "0030_ragjob_one_active_conversation")]

    operations = [
        migrations.AlterField(
            model_name="ragjob",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("processing", "Processing"),
                    ("completed", "Completed"),
                    ("failed", "Failed"),
                    ("canceled", "Canceled"),
                    ("interrupted", "Interrupted"),
                ],
                db_index=True,
                default="pending",
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="ragjob",
            name="stage",
            field=models.CharField(
                choices=[
                    ("queued", "Queued"),
                    ("searching", "Searching"),
                    ("generating", "Generating"),
                    ("completed", "Completed"),
                    ("failed", "Failed"),
                    ("canceled", "Canceled"),
                    ("interrupted", "Interrupted"),
                ],
                db_index=True,
                default="queued",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="ragjob",
            name="deadline_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="ragjob",
            name="lease_expires_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="ragjob",
            name="lease_heartbeat_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="ragjob",
            name="interrupted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(backfill_active_execution_leases, migrations.RunPython.noop),
    ]

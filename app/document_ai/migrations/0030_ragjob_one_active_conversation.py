from django.db import migrations, models
from django.db.models import Count
from django.utils import timezone


def close_duplicate_active_jobs(apps, schema_editor):
    rag_job = apps.get_model("document_ai", "RAGJob")
    duplicates = (
        rag_job.objects.filter(
            conversation_id__isnull=False,
            status__in=["pending", "processing"],
        )
        .values("conversation_id")
        .annotate(active_count=Count("id"))
        .filter(active_count__gt=1)
    )
    now = timezone.now()
    for duplicate in duplicates.iterator():
        active_ids = list(
            rag_job.objects.filter(
                conversation_id=duplicate["conversation_id"],
                status__in=["pending", "processing"],
            )
            .order_by("-created_at", "-id")
            .values_list("id", flat=True)
        )
        rag_job.objects.filter(id__in=active_ids[1:]).update(
            status="failed",
            stage="failed",
            stage_message="중복 활성 세션 작업을 정리했습니다.",
            error_message="Superseded while enforcing one active answer per conversation.",
            completed_at=now,
            updated_at=now,
        )


class Migration(migrations.Migration):

    dependencies = [
        ("document_ai", "0029_executor_job_receipts"),
    ]

    operations = [
        migrations.RunPython(close_duplicate_active_jobs, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="ragjob",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    ("conversation__isnull", False),
                    ("status__in", ["pending", "processing"]),
                ),
                fields=("conversation",),
                name="docai_rag_one_active_conversation",
            ),
        ),
    ]

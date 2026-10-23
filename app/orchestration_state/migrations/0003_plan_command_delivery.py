from django.db import migrations, models
import django.db.models.deletion


def backfill(apps, schema_editor):
    Plan = apps.get_model("orchestration_state", "PlanSubmission")
    Delivery = apps.get_model("orchestration_state", "PlanCommandDelivery")
    alias = schema_editor.connection.alias
    for row in Plan.objects.using(alias).iterator():
        for index, step in enumerate(row.definition["steps"]):
            Delivery.objects.using(alias).create(plan_id=row.pk, step_id=step["step_id"],
                job_id=row.definition["step_jobs"][step["step_id"]],
                published_at=row.published_at if index == 0 else None,
                publish_error=row.publish_error if index == 0 else "")


class Migration(migrations.Migration):
    dependencies = [("orchestration_state", "0002_submission_control")]
    operations = [
        migrations.CreateModel(name="PlanCommandDelivery", fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("step_id", models.CharField(max_length=64)),
            ("job_id", models.CharField(max_length=64, unique=True)),
            ("published_at", models.DateTimeField(null=True, blank=True)),
            ("publish_error", models.TextField(default="", blank=True)),
            ("plan", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                related_name="commands", to="orchestration_state.plansubmission")),
        ], options={"constraints": [models.UniqueConstraint(fields=("plan", "step_id"), name="plan_command_step_unique")]}),
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]

from django.db import migrations, models


def source_reference(apps, schema_editor):
    Plan = apps.get_model("documents", "PlanSubmission")
    for row in Plan.objects.using(schema_editor.connection.alias).select_related("source").iterator():
        row.source_uid = str(row.source.uid)
        row.save(using=schema_editor.connection.alias, update_fields=["source_uid"])


class Migration(migrations.Migration):
    dependencies = [("documents", "0003_rename_plan_submission")]
    operations = [
        migrations.AddField("plansubmission", "source_uid", models.CharField(max_length=64, default=""), preserve_default=False),
        migrations.RunPython(source_reference),
        migrations.RemoveField("plansubmission", "source"),
    ]

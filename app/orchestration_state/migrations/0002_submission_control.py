from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("orchestration_state", "0001_initial"),
        ("documents", "0005_transfer_plan_state"),
    ]
    operations = [migrations.AddField(
        model_name="plansubmission", name="control",
        field=models.JSONField(default=dict, blank=True),
    )]

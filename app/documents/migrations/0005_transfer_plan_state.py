from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("documents", "0004_plan_source_reference"),
        ("orchestration_state", "0001_initial"),
    ]
    operations = [migrations.SeparateDatabaseAndState(
        database_operations=[], state_operations=[migrations.DeleteModel("PlanSubmission")],
    )]

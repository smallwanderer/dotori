from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("documents", "0002_documentplansubmission")]

    operations = [
        migrations.RenameModel(
            old_name="DocumentPlanSubmission",
            new_name="PlanSubmission",
        ),
    ]

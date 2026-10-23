from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("documents", "0005_transfer_plan_state")]
    operations = [migrations.AddField(
        model_name="documentparseresult", name="parse_revision",
        field=models.UUIDField(null=True, blank=True, editable=False),
    )]

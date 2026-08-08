from django.contrib.postgres.operations import CreateExtension
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("document_ai", "0017_fix_hnsw_opclass_to_vector_ip_ops"),
    ]

    operations = [
        CreateExtension("pg_stat_statements"),
        migrations.AddField(
            model_name="searchjob",
            name="performance_metrics",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="ragjob",
            name="performance_metrics",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]

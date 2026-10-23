from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("documents", "0007_embedding_manifest")]
    operations = [migrations.AddField(
        model_name="chunkembedding", name="provenance",
        field=models.JSONField(null=True, blank=True, default=None),
    ), migrations.AddField(
        model_name="chunksegmentembedding", name="provenance",
        field=models.JSONField(null=True, blank=True, default=None),
    )]

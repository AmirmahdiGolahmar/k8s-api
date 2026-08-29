import django.db.models.deletion
from django.db import migrations, models

import backups.models


class Migration(migrations.Migration):

    dependencies = [
        ("clusters", "0004_app"),
        ("backups", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="backup",
            name="app",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="backups",
                to="clusters.app",
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="backup",
            name="source_path",
            field=models.CharField(default="", max_length=500),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="backup",
            name="pod_name",
            field=models.CharField(blank=True, max_length=253),
        ),
        migrations.AddField(
            model_name="backup",
            name="public_id",
            field=models.CharField(
                default=backups.models.generate_public_id,
                editable=False,
                max_length=20,
                unique=True,
            ),
        ),
        migrations.AlterField(
            model_name="backup",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("running", "Running"),
                    ("completed", "Completed"),
                    ("failed", "Failed"),
                ],
                default="pending",
                max_length=20,
            ),
        ),
    ]

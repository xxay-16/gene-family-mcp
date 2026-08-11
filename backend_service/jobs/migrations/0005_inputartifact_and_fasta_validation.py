import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('jobs', '0004_analysisjob_lease_expires_at'),
    ]

    operations = [
        migrations.AlterField(
            model_name='analysisjob',
            name='analysis_type',
            field=models.CharField(
                choices=[
                    ('cis_elements', 'Cis-element analysis'),
                    ('fasta_validation', 'FASTA validation and normalization'),
                ],
                max_length=64,
            ),
        ),
        migrations.CreateModel(
            name='InputArtifact',
            fields=[
                (
                    'id',
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ('kind', models.CharField(max_length=64)),
                ('filename', models.CharField(max_length=255)),
                ('storage_path', models.CharField(max_length=1024, unique=True)),
                (
                    'media_type',
                    models.CharField(
                        default='application/octet-stream',
                        max_length=255,
                    ),
                ),
                ('size', models.PositiveBigIntegerField()),
                ('sha256', models.CharField(db_index=True, max_length=64)),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'ordering': ['-created_at'],
                'constraints': [
                    models.UniqueConstraint(
                        fields=('kind', 'sha256'),
                        name='unique_input_artifact_content',
                    )
                ],
            },
        ),
    ]

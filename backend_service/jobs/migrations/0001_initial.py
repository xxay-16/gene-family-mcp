import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name='AnalysisJob',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('analysis_type', models.CharField(choices=[('cis_elements', 'Cis-element analysis')], max_length=64)),
                ('status', models.CharField(choices=[('queued', 'Queued'), ('running', 'Running'), ('waiting_external', 'Waiting for external result'), ('succeeded', 'Succeeded'), ('failed', 'Failed'), ('cancelled', 'Cancelled')], db_index=True, default='queued', max_length=32)),
                ('stage', models.CharField(default='queued', max_length=64)),
                ('progress', models.PositiveSmallIntegerField(blank=True, null=True)),
                ('parameters', models.JSONField(default=dict)),
                ('result', models.JSONField(blank=True, default=dict)),
                ('error_code', models.CharField(blank=True, max_length=64)),
                ('error_message', models.TextField(blank=True)),
                ('queue_task_id', models.CharField(blank=True, db_index=True, max_length=64)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('started_at', models.DateTimeField(blank=True, null=True)),
                ('finished_at', models.DateTimeField(blank=True, null=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={'ordering': ['-created_at']},
        ),
        migrations.CreateModel(
            name='Artifact',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('kind', models.CharField(max_length=64)),
                ('filename', models.CharField(max_length=255)),
                ('storage_path', models.CharField(max_length=1024)),
                ('media_type', models.CharField(default='application/octet-stream', max_length=255)),
                ('size', models.PositiveBigIntegerField()),
                ('sha256', models.CharField(max_length=64)),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('job', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='artifacts', to='jobs.analysisjob')),
            ],
            options={'ordering': ['created_at', 'filename']},
        ),
        migrations.CreateModel(
            name='AnalysisEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('event_type', models.CharField(max_length=64)),
                ('message', models.TextField(blank=True)),
                ('details', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('job', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='events', to='jobs.analysisjob')),
            ],
            options={'ordering': ['created_at', 'id']},
        ),
    ]

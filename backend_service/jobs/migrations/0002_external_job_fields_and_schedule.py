from django.db import migrations, models


SCHEDULE_NAME = 'gene-family-poll-external-results'
SCHEDULE_FUNC = 'jobs.tasks.poll_waiting_external_jobs'


def create_poll_schedule(apps, schema_editor):
    Schedule = apps.get_model('django_q', 'Schedule')
    Schedule.objects.update_or_create(
        name=SCHEDULE_NAME,
        defaults={
            'func': SCHEDULE_FUNC,
            'schedule_type': 'I',
            'minutes': 1,
            'repeats': -1,
            'cluster': 'gene_family_backend',
        },
    )


def delete_poll_schedule(apps, schema_editor):
    Schedule = apps.get_model('django_q', 'Schedule')
    Schedule.objects.filter(name=SCHEDULE_NAME, func=SCHEDULE_FUNC).delete()


class Migration(migrations.Migration):
    dependencies = [
        ('django_q', '0018_task_success_index'),
        ('jobs', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='analysisjob',
            name='external_deadline',
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name='analysisjob',
            name='last_polled_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='analysisjob',
            name='provider_ref',
            field=models.CharField(blank=True, db_index=True, max_length=128),
        ),
        migrations.AddConstraint(
            model_name='artifact',
            constraint=models.UniqueConstraint(
                fields=('job', 'storage_path'),
                name='unique_job_artifact_path',
            ),
        ),
        migrations.RunPython(create_poll_schedule, delete_poll_schedule),
    ]

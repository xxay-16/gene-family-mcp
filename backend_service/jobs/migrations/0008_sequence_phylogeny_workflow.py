import django.db.models.deletion
from django.db import migrations, models

SCHEDULE_NAME = 'gene-family-advance-workflows'
SCHEDULE_FUNC = 'jobs.tasks.advance_waiting_workflows'


def create_workflow_schedule(apps, schema_editor):
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


def delete_workflow_schedule(apps, schema_editor):
    Schedule = apps.get_model('django_q', 'Schedule')
    Schedule.objects.filter(name=SCHEDULE_NAME, func=SCHEDULE_FUNC).delete()


class Migration(migrations.Migration):
    dependencies = [
        ('django_q', '0018_task_success_index'),
        ('jobs', '0007_phylogenetic_tree'),
    ]

    operations = [
        migrations.AlterField(
            model_name='analysisjob',
            name='analysis_type',
            field=models.CharField(
                choices=[
                    ('cis_elements', 'Cis-element analysis'),
                    ('fasta_validation', 'FASTA validation and normalization'),
                    ('multiple_sequence_alignment', 'Multiple sequence alignment'),
                    ('phylogenetic_tree', 'Phylogenetic tree'),
                    ('sequence_phylogeny', 'Sequence phylogeny workflow'),
                ],
                max_length=64,
            ),
        ),
        migrations.AlterField(
            model_name='analysisjob',
            name='status',
            field=models.CharField(
                choices=[
                    ('queued', 'Queued'),
                    ('running', 'Running'),
                    ('waiting_external', 'Waiting for external result'),
                    ('waiting_dependency', 'Waiting for dependency'),
                    ('succeeded', 'Succeeded'),
                    ('failed', 'Failed'),
                    ('cancelled', 'Cancelled'),
                ],
                db_index=True,
                default='queued',
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name='analysisjob',
            name='parent_job',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='child_jobs',
                to='jobs.analysisjob',
            ),
        ),
        migrations.AddField(
            model_name='analysisjob',
            name='workflow_step',
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddConstraint(
            model_name='analysisjob',
            constraint=models.UniqueConstraint(
                condition=models.Q(parent_job__isnull=False)
                & ~models.Q(workflow_step=''),
                fields=('parent_job', 'workflow_step'),
                name='unique_workflow_job_step',
            ),
        ),
        migrations.RunPython(create_workflow_schedule, delete_workflow_schedule),
    ]

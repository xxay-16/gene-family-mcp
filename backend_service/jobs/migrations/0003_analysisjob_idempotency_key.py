from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('jobs', '0002_external_job_fields_and_schedule')]

    operations = [
        migrations.AddField(
            model_name='analysisjob',
            name='idempotency_key',
            field=models.CharField(blank=True, db_index=True, max_length=128),
        ),
        migrations.AddConstraint(
            model_name='analysisjob',
            constraint=models.UniqueConstraint(
                condition=~models.Q(idempotency_key=''),
                fields=('analysis_type', 'idempotency_key'),
                name='unique_analysis_idempotency_key',
            ),
        ),
    ]

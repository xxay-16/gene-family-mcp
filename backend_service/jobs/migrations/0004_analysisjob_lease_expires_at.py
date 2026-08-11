from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('jobs', '0003_analysisjob_idempotency_key')]

    operations = [
        migrations.AddField(
            model_name='analysisjob',
            name='lease_expires_at',
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
    ]

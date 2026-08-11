from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('jobs', '0005_inputartifact_and_fasta_validation'),
    ]

    operations = [
        migrations.AlterField(
            model_name='analysisjob',
            name='analysis_type',
            field=models.CharField(
                choices=[
                    ('cis_elements', 'Cis-element analysis'),
                    ('fasta_validation', 'FASTA validation and normalization'),
                    (
                        'multiple_sequence_alignment',
                        'Multiple sequence alignment',
                    ),
                ],
                max_length=64,
            ),
        ),
    ]

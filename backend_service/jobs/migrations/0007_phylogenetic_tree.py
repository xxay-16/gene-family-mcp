from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('jobs', '0006_multiple_sequence_alignment'),
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
                    ('phylogenetic_tree', 'Phylogenetic tree'),
                ],
                max_length=64,
            ),
        ),
    ]

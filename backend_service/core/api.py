from django.db import connection
from django_q.models import Schedule
from jobs.local_tools.capabilities import fasttree_capability, mafft_capability
from ninja import Router
from ninja.responses import Response

router = Router(tags=['core'])


@router.get('/health')
def health(request):
    return {'status': 'ok', 'service': 'gene-family-backend'}


@router.get('/ready')
def ready(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
            cursor.fetchone()
        active_schedule_names = set(
            Schedule.objects.filter(
                name__in=[
                    'gene-family-poll-external-results',
                    'gene-family-advance-workflows',
                ],
            )
            .exclude(repeats=0)
            .values_list('name', flat=True)
        )
    except Exception:
        return Response(
            {'status': 'not_ready', 'service': 'gene-family-backend'},
            status=503,
        )
    required_schedule_names = {
        'gene-family-poll-external-results',
        'gene-family-advance-workflows',
    }
    missing_schedules = sorted(required_schedule_names - active_schedule_names)
    if missing_schedules:
        return Response(
            {
                'status': 'not_ready',
                'service': 'gene-family-backend',
                'reason': 'required django-q2 schedules are missing',
                'missing_schedules': missing_schedules,
            },
            status=503,
        )
    return {
        'status': 'ready',
        'service': 'gene-family-backend',
        'database': 'ok',
        'schedules': 'ok',
    }


@router.get('/capabilities')
def capabilities(request):
    mafft = mafft_capability()
    fasttree = fasttree_capability()
    return {
        'service': 'gene-family-backend',
        'queue_backend': 'django-q2',
        'external_result_scheduler': {
            'backend': 'django-q2 Schedule',
            'interval_seconds': 60,
        },
        'analysis_types': {
            'fasta_validation': {
                'status': 'available',
                'provider': 'local',
                'input': 'FASTA text uploaded as a content-addressed input artifact',
                'alphabets': ['auto', 'dna', 'protein'],
                'outputs': ['normalized_fasta', 'fasta_validation_summary'],
                'asynchronous': True,
            },
            'cis_elements': {
                'status': 'available',
                'provider': 'PlantCARE',
                'input': 'DNA promoter sequence',
                'alphabet': 'ACGTN',
                'asynchronous': True,
            },
            'multiple_sequence_alignment': {
                'status': 'available' if mafft['available'] else 'unavailable',
                'provider': 'MAFFT',
                'input': 'normalized_fasta Artifact from a succeeded job',
                'strategies': ['auto', 'linsi', 'ginsi', 'einsi'],
                'asynchronous': True,
                'runtime': mafft,
            },
            'phylogenetic_tree': {
                'status': (
                    'available' if fasttree['available'] else 'unavailable'
                ),
                'provider': 'FastTree',
                'input': 'aligned_fasta Artifact from a succeeded job',
                'dna_models': ['auto', 'gtr', 'jc'],
                'protein_models': ['auto', 'jtt', 'lg', 'wag'],
                'asynchronous': True,
                'runtime': fasttree,
            },
            'sequence_phylogeny': {
                'status': (
                    'available'
                    if mafft['available'] and fasttree['available']
                    else 'unavailable'
                ),
                'provider': 'django-q2 workflow',
                'input': 'FASTA text uploaded as an input Artifact',
                'steps': [
                    'fasta_validation',
                    'multiple_sequence_alignment',
                    'phylogenetic_tree',
                ],
                'asynchronous': True,
                'runtime': {
                    'mafft': mafft,
                    'fasttree': fasttree,
                },
            },
        },
    }

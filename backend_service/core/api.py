from django.db import connection
from django_q.models import Schedule
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
        schedule_exists = Schedule.objects.filter(
            name='gene-family-poll-external-results',
        ).exclude(repeats=0).exists()
    except Exception:
        return Response(
            {'status': 'not_ready', 'service': 'gene-family-backend'},
            status=503,
        )
    if not schedule_exists:
        return Response(
            {
                'status': 'not_ready',
                'service': 'gene-family-backend',
                'reason': 'external result schedule is missing',
            },
            status=503,
        )
    return {
        'status': 'ready',
        'service': 'gene-family-backend',
        'database': 'ok',
        'external_result_schedule': 'ok',
    }


@router.get('/capabilities')
def capabilities(request):
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
            }
        },
    }

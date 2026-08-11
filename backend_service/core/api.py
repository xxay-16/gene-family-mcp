from ninja import Router

router = Router(tags=['core'])


@router.get('/health')
def health(request):
    return {'status': 'ok', 'service': 'gene-family-backend'}


@router.get('/capabilities')
def capabilities(request):
    return {
        'service': 'gene-family-backend',
        'queue_backend': 'django-q2',
        'analysis_types': {
            'cis_elements': {
                'status': 'available',
                'provider': 'PlantCARE',
                'input': 'DNA promoter sequence',
                'alphabet': 'ACGTN',
                'asynchronous': True,
            }
        },
    }

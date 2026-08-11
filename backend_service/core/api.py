from ninja import Router

router = Router(tags=['core'])


@router.get('/health')
def health(request):
    return {'status': 'ok', 'service': 'gene-family-backend'}

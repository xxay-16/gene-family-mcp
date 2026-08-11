import secrets
import uuid

from django.conf import settings
from django.http import JsonResponse


class RequestIdMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = request.headers.get('X-Request-ID', '').strip()
        if not request_id or len(request_id) > 128:
            request_id = str(uuid.uuid4())
        request.request_id = request_id
        response = self.get_response(request)
        response['X-Request-ID'] = request_id
        return response


class BackendTokenMiddleware:
    exempt_paths = {
        '/api/core/health',
        '/api/docs',
        '/api/openapi.json',
    }

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        expected_token = settings.BACKEND_API_TOKEN
        if (
            expected_token
            and request.path.startswith('/api/')
            and request.path not in self.exempt_paths
        ):
            authorization = request.headers.get('Authorization', '')
            scheme, separator, token = authorization.partition(' ')
            if (
                not separator
                or scheme.lower() != 'bearer'
                or not secrets.compare_digest(token, expected_token)
            ):
                return JsonResponse(
                    {
                        'error': {
                            'code': 'UNAUTHORIZED',
                            'message': 'A valid backend bearer token is required',
                        }
                    },
                    status=401,
                )
        return self.get_response(request)

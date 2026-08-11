import io
import json
from unittest import TestCase
from unittest.mock import patch

from mcp_server.backend_client import BackendAPIError, BackendClient


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return json.dumps(self.payload).encode('utf-8')


class BackendClientTests(TestCase):
    @patch('mcp_server.backend_client.request.urlopen')
    def test_submit_calls_backend_api(self, urlopen_mock):
        urlopen_mock.return_value = _Response(
            {'task_id': 'task-123', 'status': 'queued'}
        )
        client = BackendClient('http://backend.test/api')

        result = client.submit_cis_element_analysis('ACGT')

        self.assertEqual(result['task_id'], 'task-123')
        api_request = urlopen_mock.call_args.args[0]
        self.assertEqual(
            api_request.full_url,
            'http://backend.test/api/cis-elements/submit',
        )
        self.assertEqual(json.loads(api_request.data), {'sequence': 'ACGT'})

    @patch('mcp_server.backend_client.request.urlopen')
    def test_backend_http_error_is_structured(self, urlopen_mock):
        from urllib.error import HTTPError

        urlopen_mock.side_effect = HTTPError(
            url='http://backend.test/api/jobs/missing',
            code=404,
            msg='Not Found',
            hdrs=None,
            fp=io.BytesIO(b'{"error": "task not found"}'),
        )
        client = BackendClient('http://backend.test/api')

        with self.assertRaises(BackendAPIError) as raised:
            client.get_task_status('missing')

        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(raised.exception.detail, {'error': 'task not found'})

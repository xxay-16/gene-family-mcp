import io
import json
from unittest import TestCase
from unittest.mock import patch

from mcp_server import server
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
    def test_submit_uses_generic_jobs_api(self, urlopen_mock):
        urlopen_mock.return_value = _Response(
            {'job_id': 'job-123', 'status': 'queued'}
        )
        client = BackendClient(
            'http://backend.test/api',
            token='backend-secret',
        )

        result = client.submit_cis_element_analysis('ACGT', 'request-123')

        self.assertEqual(result['job_id'], 'job-123')
        api_request = urlopen_mock.call_args.args[0]
        self.assertEqual(api_request.full_url, 'http://backend.test/api/jobs')
        self.assertEqual(
            json.loads(api_request.data),
            {
                'analysis_type': 'cis_elements',
                'parameters': {'sequence': 'ACGT'},
            },
        )
        self.assertEqual(
            api_request.headers['Authorization'],
            'Bearer backend-secret',
        )
        self.assertEqual(api_request.headers['Idempotency-key'], 'request-123')

    @patch('mcp_server.backend_client.request.urlopen')
    def test_job_operations_use_stable_contract(self, urlopen_mock):
        urlopen_mock.return_value = _Response({'job_id': 'job-123'})
        client = BackendClient('http://backend.test/api')

        client.get_job('job-123')
        self.assertEqual(
            urlopen_mock.call_args.args[0].full_url,
            'http://backend.test/api/jobs/job-123',
        )
        client.get_job_result('job-123')
        self.assertEqual(
            urlopen_mock.call_args.args[0].full_url,
            'http://backend.test/api/jobs/job-123/result',
        )
        client.cancel_job('job-123')
        cancel_request = urlopen_mock.call_args.args[0]
        self.assertEqual(
            cancel_request.full_url,
            'http://backend.test/api/jobs/job-123/cancel',
        )
        self.assertEqual(cancel_request.method, 'POST')

    @patch('mcp_server.backend_client.request.urlopen')
    def test_fasta_submission_uploads_input_then_creates_job(self, urlopen_mock):
        urlopen_mock.side_effect = [
            _Response(
                {
                    'input_artifact_id': 'input-123',
                    'sha256': 'a' * 64,
                    'created': True,
                }
            ),
            _Response({'job_id': 'job-456', 'status': 'queued'}),
        ]
        client = BackendClient('http://backend.test/api')

        result = client.submit_fasta_validation(
            '>gene1\nACGT\n',
            alphabet='dna',
            filename='genes.fa',
            idempotency_key='fasta-request-1',
        )

        self.assertEqual(result['job_id'], 'job-456')
        self.assertEqual(result['input_artifact']['input_artifact_id'], 'input-123')
        upload_request, job_request = [
            call.args[0] for call in urlopen_mock.call_args_list
        ]
        self.assertEqual(
            upload_request.full_url,
            'http://backend.test/api/inputs/fasta',
        )
        self.assertEqual(
            json.loads(upload_request.data),
            {'content': '>gene1\nACGT\n', 'filename': 'genes.fa'},
        )
        self.assertEqual(job_request.full_url, 'http://backend.test/api/jobs')
        self.assertEqual(
            json.loads(job_request.data),
            {
                'analysis_type': 'fasta_validation',
                'parameters': {
                    'input_artifact_id': 'input-123',
                    'alphabet': 'dna',
                },
            },
        )
        self.assertEqual(
            job_request.headers['Idempotency-key'],
            'fasta-request-1',
        )

    @patch('mcp_server.backend_client.request.urlopen')
    def test_backend_http_error_is_structured(self, urlopen_mock):
        from urllib.error import HTTPError

        urlopen_mock.side_effect = HTTPError(
            url='http://backend.test/api/jobs/missing',
            code=404,
            msg='Not Found',
            hdrs=None,
            fp=io.BytesIO(b'{"error": {"code": "JOB_NOT_FOUND"}}'),
        )
        client = BackendClient('http://backend.test/api')

        with self.assertRaises(BackendAPIError) as raised:
            client.get_job('missing')

        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(
            raised.exception.detail,
            {'error': {'code': 'JOB_NOT_FOUND'}},
        )


class MCPToolTests(TestCase):
    @patch.object(server.backend, 'health', return_value={'status': 'ok'})
    @patch.object(server.backend, 'capabilities', return_value={'analysis_types': {}})
    def test_health_and_capability_tools(self, capabilities_mock, health_mock):
        self.assertEqual(server.backend_health(), {'status': 'ok'})
        self.assertEqual(server.get_capabilities(), {'analysis_types': {}})
        health_mock.assert_called_once()
        capabilities_mock.assert_called_once()

    @patch.object(server.backend, 'submit_cis_element_analysis')
    @patch.object(server.backend, 'submit_fasta_validation')
    @patch.object(server.backend, 'get_job')
    @patch.object(server.backend, 'get_job_result')
    @patch.object(server.backend, 'cancel_job')
    def test_job_tools_delegate_to_backend(
        self,
        cancel_mock,
        result_mock,
        status_mock,
        fasta_mock,
        submit_mock,
    ):
        submit_mock.return_value = {'job_id': 'job-1'}
        fasta_mock.return_value = {'job_id': 'job-2'}
        status_mock.return_value = {'status': 'queued'}
        result_mock.return_value = {'result': {}}
        cancel_mock.return_value = {'status': 'cancelled'}

        self.assertEqual(
            server.submit_cis_element_analysis('ACGT', 'key-1'),
            {'job_id': 'job-1'},
        )
        self.assertEqual(
            server.validate_fasta(
                '>gene1\nACGT\n',
                'dna',
                'genes.fa',
                'key-2',
            ),
            {'job_id': 'job-2'},
        )
        self.assertEqual(server.get_job_status('job-1'), {'status': 'queued'})
        self.assertEqual(server.get_job_result('job-1'), {'result': {}})
        self.assertEqual(server.cancel_job('job-1'), {'status': 'cancelled'})
        submit_mock.assert_called_once_with('ACGT', 'key-1')
        fasta_mock.assert_called_once_with(
            '>gene1\nACGT\n',
            'dna',
            'genes.fa',
            'key-2',
        )

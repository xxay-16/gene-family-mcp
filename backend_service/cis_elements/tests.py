import uuid
from unittest.mock import patch

from django.test import Client, TestCase
from jobs.models import AnalysisJob


class CisElementAPITests(TestCase):
    def setUp(self):
        self.client = Client()

    @patch('jobs.services.async_task', return_value='q2-task-123')
    def test_submit_creates_business_job_and_normalizes_sequence(
        self,
        async_task_mock,
    ):
        response = self.client.post(
            '/api/cis-elements/submit',
            data={'sequence': 'acgt nn\n'},
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        job = AnalysisJob.objects.get(id=payload['job_id'])
        self.assertEqual(payload['task_id'], str(job.id))
        self.assertEqual(payload['status'], AnalysisJob.Status.QUEUED)
        self.assertEqual(job.parameters['sequence'], 'ACGTNN')
        self.assertEqual(job.queue_task_id, 'q2-task-123')
        async_task_mock.assert_called_once_with(
            'jobs.tasks.execute_analysis_job',
            str(job.id),
            task_name=f'analysis-job-{job.id}',
            save=False,
        )

    def test_submit_rejects_invalid_sequence(self):
        response = self.client.post(
            '/api/cis-elements/submit',
            data={'sequence': 'ACGT-X'},
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['error']['code'], 'INVALID_SEQUENCE')
        self.assertFalse(AnalysisJob.objects.exists())

    def test_unknown_task_returns_404(self):
        task_id = uuid.uuid4()
        response = self.client.get(f'/api/cis-elements/tasks/{task_id}')

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()['status'], 'not_found')
        self.assertEqual(response.json()['error']['code'], 'JOB_NOT_FOUND')

import hashlib
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch

from django.test import Client, TestCase, override_settings
from django_q.models import OrmQ
from django_q.signing import SignedPackage

from .models import AnalysisEvent, AnalysisJob, Artifact
from .services import create_analysis_job
from .tasks import execute_analysis_job


class JobAPITests(TestCase):
    def setUp(self):
        self.client = Client()

    @patch('jobs.services.async_task', return_value='q2-task-456')
    def test_create_and_query_job(self, _async_task_mock):
        response = self.client.post(
            '/api/jobs',
            data={
                'analysis_type': 'cis_elements',
                'parameters': {'sequence': 'ac gt'},
            },
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 202)
        created = response.json()
        status_response = self.client.get(created['status_url'])
        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(status_response.json()['job_id'], created['job_id'])
        self.assertEqual(status_response.json()['status'], 'queued')

    def test_result_is_unavailable_before_success(self):
        job = AnalysisJob.objects.create(
            analysis_type=AnalysisJob.AnalysisType.CIS_ELEMENTS,
            parameters={'sequence': 'ACGT'},
        )

        response = self.client.get(f'/api/jobs/{job.id}/result')

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['error']['code'], 'JOB_NOT_COMPLETE')

    def test_cancel_queued_job(self):
        job = AnalysisJob.objects.create(
            analysis_type=AnalysisJob.AnalysisType.CIS_ELEMENTS,
            parameters={'sequence': 'ACGT'},
        )

        response = self.client.post(f'/api/jobs/{job.id}/cancel')

        self.assertEqual(response.status_code, 200)
        job.refresh_from_db()
        self.assertEqual(job.status, AnalysisJob.Status.CANCELLED)
        self.assertTrue(job.events.filter(event_type='job_cancelled').exists())

    def test_unknown_job_returns_404(self):
        response = self.client.get(f'/api/jobs/{uuid.uuid4()}')

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()['error']['code'], 'JOB_NOT_FOUND')


class JobExecutionTests(TestCase):
    def test_business_job_is_enqueued_in_django_q2(self):
        job = create_analysis_job(
            AnalysisJob.AnalysisType.CIS_ELEMENTS,
            {'sequence': 'ACGT'},
        )

        queued = OrmQ.objects.get()
        payload = SignedPackage.loads(queued.payload)
        self.assertEqual(payload['func'], 'jobs.tasks.execute_analysis_job')
        self.assertEqual(payload['args'], (str(job.id),))
        self.assertEqual(payload['id'], job.queue_task_id)
        self.assertFalse(payload['save'])

    @override_settings()
    @patch('jobs.tasks.run_prediction')
    def test_worker_persists_result_artifact_and_events(self, run_prediction_mock):
        with tempfile.TemporaryDirectory() as temp_dir:
            artifact_root = Path(temp_dir)
            job = AnalysisJob.objects.create(
                analysis_type=AnalysisJob.AnalysisType.CIS_ELEMENTS,
                parameters={'sequence': 'ACGT'},
            )
            output_dir = artifact_root / str(job.id)
            output_dir.mkdir(parents=True)
            result_file = output_dir / 'plantcare.tab'
            result_file.write_text('motif\tcount\nTATA-box\t2\n', encoding='utf-8')

            def provider(sequence, output_dir, on_submitted, on_result_received):
                self.assertEqual(sequence, 'ACGT')
                self.assertEqual(Path(output_dir), artifact_root / str(job.id))
                on_submitted('provider-ref')
                on_result_received()
                return {
                    'ref': 'provider-ref',
                    'subject': 'PlantCARE result',
                    'date': 'today',
                    'attachments': [str(result_file)],
                }

            run_prediction_mock.side_effect = provider
            with override_settings(ARTIFACT_ROOT=artifact_root):
                result = execute_analysis_job(str(job.id))

            job.refresh_from_db()
            artifact = Artifact.objects.get(job=job)
            self.assertEqual(result['status'], AnalysisJob.Status.SUCCEEDED)
            self.assertEqual(job.status, AnalysisJob.Status.SUCCEEDED)
            self.assertEqual(job.progress, 100)
            self.assertNotIn(str(artifact_root), str(job.result))
            self.assertEqual(artifact.filename, 'plantcare.tab')
            self.assertEqual(
                artifact.sha256,
                hashlib.sha256(result_file.read_bytes()).hexdigest(),
            )
            self.assertTrue(
                AnalysisEvent.objects.filter(
                    job=job,
                    event_type='provider_submitted',
                ).exists()
            )
            self.assertTrue(
                AnalysisEvent.objects.filter(
                    job=job,
                    event_type='job_succeeded',
                ).exists()
            )

    @patch('jobs.tasks.run_prediction', side_effect=TimeoutError('timeout'))
    def test_worker_records_safe_failure(self, _run_prediction_mock):
        job = AnalysisJob.objects.create(
            analysis_type=AnalysisJob.AnalysisType.CIS_ELEMENTS,
            parameters={'sequence': 'ACGT'},
        )

        result = execute_analysis_job(str(job.id))

        job.refresh_from_db()
        self.assertEqual(result['status'], AnalysisJob.Status.FAILED)
        self.assertEqual(job.error_code, 'PROVIDER_TIMEOUT')
        self.assertNotIn('Traceback', job.error_message)
        self.assertTrue(job.events.filter(event_type='job_failed').exists())

import hashlib
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.test import Client, TestCase, override_settings
from django.utils import timezone
from django_q.models import OrmQ, Schedule
from django_q.signing import SignedPackage

from .models import AnalysisEvent, AnalysisJob, Artifact
from .services import create_analysis_job
from .tasks import execute_analysis_job, poll_waiting_external_jobs


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

    def test_completed_result_returns_artifact_manifest(self):
        job = AnalysisJob.objects.create(
            analysis_type=AnalysisJob.AnalysisType.CIS_ELEMENTS,
            parameters={'sequence': 'ACGT'},
            status=AnalysisJob.Status.SUCCEEDED,
            stage='completed',
            progress=100,
            result={'summary': {'record_count': 2}},
        )
        Artifact.objects.create(
            job=job,
            kind='plantcare_structured_result',
            filename='plantcare_result.json',
            storage_path=f'{job.id}/plantcare_result.json',
            media_type='application/json',
            size=10,
            sha256='a' * 64,
        )

        response = self.client.get(f'/api/jobs/{job.id}/result')

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['result']['summary']['record_count'], 2)
        self.assertEqual(
            payload['artifacts'][0]['kind'],
            'plantcare_structured_result',
        )
        self.assertIn('/api/artifacts/', payload['artifacts'][0]['download_url'])

    def test_artifact_download_uses_storage_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            artifact_root = Path(temp_dir)
            job = AnalysisJob.objects.create(
                analysis_type=AnalysisJob.AnalysisType.CIS_ELEMENTS,
                parameters={'sequence': 'ACGT'},
                status=AnalysisJob.Status.SUCCEEDED,
            )
            file_path = artifact_root / str(job.id) / 'result.json'
            file_path.parent.mkdir(parents=True)
            file_path.write_text('{"ok": true}', encoding='utf-8')
            artifact = Artifact.objects.create(
                job=job,
                kind='plantcare_structured_result',
                filename='result.json',
                storage_path=str(file_path.relative_to(artifact_root)),
                media_type='application/json',
                size=file_path.stat().st_size,
                sha256='a' * 64,
            )

            with override_settings(ARTIFACT_ROOT=artifact_root):
                response = self.client.get(
                    f'/api/artifacts/{artifact.id}/download'
                )
                body = b''.join(response.streaming_content)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(body, b'{"ok": true}')
        self.assertIn('attachment;', response.headers['Content-Disposition'])

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

    def test_list_and_event_audit_endpoints(self):
        job = AnalysisJob.objects.create(
            analysis_type=AnalysisJob.AnalysisType.CIS_ELEMENTS,
            parameters={'sequence': 'ACGT'},
        )
        AnalysisEvent.objects.create(
            job=job,
            event_type='job_created',
            message='created',
        )

        list_response = self.client.get('/api/jobs?status=queued&limit=10')
        event_response = self.client.get(f'/api/jobs/{job.id}/events')

        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(list_response.json()['jobs'][0]['job_id'], str(job.id))
        self.assertEqual(event_response.status_code, 200)
        self.assertEqual(
            event_response.json()['events'][0]['event_type'],
            'job_created',
        )


class JobExecutionTests(TestCase):
    def setUp(self):
        self.client = Client()

    def test_business_job_is_enqueued_in_django_q2(self):
        job, created = create_analysis_job(
            AnalysisJob.AnalysisType.CIS_ELEMENTS,
            {'sequence': 'ACGT'},
        )

        self.assertTrue(created)
        queued = OrmQ.objects.get()
        payload = SignedPackage.loads(queued.payload)
        self.assertEqual(payload['func'], 'jobs.tasks.execute_analysis_job')
        self.assertEqual(payload['args'], (str(job.id),))
        self.assertEqual(payload['id'], job.queue_task_id)
        self.assertFalse(payload['save'])

    @patch('jobs.services.async_task', return_value='q2-task-idempotent')
    def test_idempotency_key_reuses_business_job(self, async_task_mock):
        first_job, first_created = create_analysis_job(
            AnalysisJob.AnalysisType.CIS_ELEMENTS,
            {'sequence': 'ACGT'},
            idempotency_key='request-123',
        )
        second_job, second_created = create_analysis_job(
            AnalysisJob.AnalysisType.CIS_ELEMENTS,
            {'sequence': 'ACGT'},
            idempotency_key='request-123',
        )

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first_job.id, second_job.id)
        self.assertEqual(first_job.parameters['sequence'], 'ACGT')
        async_task_mock.assert_called_once()

    @patch('jobs.services.async_task', return_value='q2-task-idempotent')
    def test_idempotency_key_rejects_different_parameters(self, _async_task_mock):
        create_analysis_job(
            AnalysisJob.AnalysisType.CIS_ELEMENTS,
            {'sequence': 'ACGT'},
            idempotency_key='request-123',
        )

        response = self.client.post(
            '/api/jobs',
            data={
                'analysis_type': 'cis_elements',
                'parameters': {'sequence': 'TTTT'},
            },
            content_type='application/json',
            HTTP_IDEMPOTENCY_KEY='request-123',
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['error']['code'], 'IDEMPOTENCY_CONFLICT')

    @override_settings(MAX_ACTIVE_JOBS=1)
    def test_capacity_limit_rejects_new_job(self):
        AnalysisJob.objects.create(
            analysis_type=AnalysisJob.AnalysisType.CIS_ELEMENTS,
            parameters={'sequence': 'ACGT'},
        )

        response = self.client.post(
            '/api/jobs',
            data={
                'analysis_type': 'cis_elements',
                'parameters': {'sequence': 'TTTT'},
            },
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['error']['code'], 'JOB_CAPACITY_REACHED')

    @override_settings(MAX_SEQUENCE_LENGTH=3)
    def test_sequence_length_limit(self):
        response = self.client.post(
            '/api/jobs',
            data={
                'analysis_type': 'cis_elements',
                'parameters': {'sequence': 'ACGT'},
            },
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 422)
        self.assertIn('maximum length', response.json()['error']['message'])

    def test_poll_schedule_is_installed_by_migration(self):
        schedule = Schedule.objects.get(name='gene-family-poll-external-results')
        self.assertEqual(schedule.func, 'jobs.tasks.poll_waiting_external_jobs')
        self.assertEqual(schedule.schedule_type, Schedule.MINUTES)
        self.assertEqual(schedule.minutes, 1)
        self.assertEqual(schedule.repeats, -1)

    @patch('jobs.tasks.submit_prediction', return_value='provider-ref')
    def test_submit_worker_releases_into_waiting_external(self, submit_mock):
        job = AnalysisJob.objects.create(
            analysis_type=AnalysisJob.AnalysisType.CIS_ELEMENTS,
            parameters={'sequence': 'ACGT'},
        )

        result = execute_analysis_job(str(job.id))

        job.refresh_from_db()
        self.assertEqual(result['status'], AnalysisJob.Status.WAITING_EXTERNAL)
        self.assertEqual(job.status, AnalysisJob.Status.WAITING_EXTERNAL)
        self.assertEqual(job.provider_ref, 'provider-ref')
        self.assertIsNotNone(job.external_deadline)
        submit_mock.assert_called_once_with('ACGT')
        self.assertTrue(job.events.filter(event_type='provider_submitted').exists())

    @patch('jobs.tasks.submit_prediction', return_value='provider-ref')
    def test_submit_worker_cannot_claim_same_job_twice(self, submit_mock):
        job = AnalysisJob.objects.create(
            analysis_type=AnalysisJob.AnalysisType.CIS_ELEMENTS,
            parameters={'sequence': 'ACGT'},
        )

        first = execute_analysis_job(str(job.id))
        second = execute_analysis_job(str(job.id))

        self.assertEqual(first['status'], AnalysisJob.Status.WAITING_EXTERNAL)
        self.assertEqual(second['status'], AnalysisJob.Status.WAITING_EXTERNAL)
        submit_mock.assert_called_once_with('ACGT')

    @override_settings(PLANTCARE_POLL_BATCH_SIZE=20)
    @patch('jobs.tasks.collect_results')
    def test_scheduler_persists_result_artifact_and_events(self, collect_mock):
        with tempfile.TemporaryDirectory() as temp_dir:
            artifact_root = Path(temp_dir)
            job = AnalysisJob.objects.create(
                analysis_type=AnalysisJob.AnalysisType.CIS_ELEMENTS,
                parameters={'sequence': 'ACGT'},
                status=AnalysisJob.Status.WAITING_EXTERNAL,
                stage='waiting_plantcare',
                provider_ref='provider-ref',
                external_deadline=timezone.now() + timedelta(minutes=30),
            )
            output_dir = artifact_root / str(job.id)
            output_dir.mkdir(parents=True)
            result_file = output_dir / 'plantcare.tab'
            result_file.write_text(
                'gene1\tTATA-box\tTATA\t10\t4\t+\tArabidopsis\tcore promoter\n',
                encoding='utf-8',
            )

            def collector(ref_to_output_dir):
                self.assertEqual(
                    ref_to_output_dir,
                    {'provider-ref': artifact_root / str(job.id)},
                )
                return {'provider-ref': {
                    'ref': 'provider-ref',
                    'subject': 'PlantCARE result',
                    'date': 'today',
                    'attachments': [str(result_file)],
                }}

            collect_mock.side_effect = collector
            with override_settings(ARTIFACT_ROOT=artifact_root):
                result = poll_waiting_external_jobs()

            job.refresh_from_db()
            artifact = Artifact.objects.get(
                job=job,
                kind='plantcare_table',
            )
            self.assertEqual(result['completed'], 1)
            self.assertEqual(job.status, AnalysisJob.Status.SUCCEEDED)
            self.assertEqual(job.progress, 100)
            self.assertNotIn(str(artifact_root), str(job.result))
            self.assertEqual(artifact.filename, 'plantcare.tab')
            self.assertEqual(job.result['summary']['record_count'], 1)
            self.assertEqual(Artifact.objects.filter(job=job).count(), 2)
            self.assertTrue(
                Artifact.objects.filter(
                    job=job,
                    kind='plantcare_structured_result',
                ).exists()
            )
            self.assertEqual(
                artifact.sha256,
                hashlib.sha256(result_file.read_bytes()).hexdigest(),
            )
            self.assertTrue(
                AnalysisEvent.objects.filter(
                    job=job,
                    event_type='job_succeeded',
                ).exists()
            )

    @patch('jobs.tasks.submit_prediction', side_effect=RuntimeError('provider down'))
    def test_submit_worker_records_safe_failure(self, _submit_mock):
        job = AnalysisJob.objects.create(
            analysis_type=AnalysisJob.AnalysisType.CIS_ELEMENTS,
            parameters={'sequence': 'ACGT'},
        )

        result = execute_analysis_job(str(job.id))

        job.refresh_from_db()
        self.assertEqual(result['status'], AnalysisJob.Status.FAILED)
        self.assertEqual(job.error_code, 'PROVIDER_EXECUTION_FAILED')
        self.assertNotIn('Traceback', job.error_message)
        self.assertTrue(job.events.filter(event_type='job_failed').exists())

    @override_settings(PLANTCARE_POLL_BATCH_SIZE=20)
    def test_scheduler_expires_waiting_job_without_calling_mailbox(self):
        job = AnalysisJob.objects.create(
            analysis_type=AnalysisJob.AnalysisType.CIS_ELEMENTS,
            parameters={'sequence': 'ACGT'},
            status=AnalysisJob.Status.WAITING_EXTERNAL,
            stage='waiting_plantcare',
            provider_ref='expired-ref',
            external_deadline=timezone.now() - timedelta(seconds=1),
        )

        with patch('jobs.tasks.collect_results') as collect_mock:
            result = poll_waiting_external_jobs()

        job.refresh_from_db()
        self.assertEqual(result['expired'], 1)
        self.assertEqual(job.status, AnalysisJob.Status.FAILED)
        self.assertEqual(job.error_code, 'PROVIDER_TIMEOUT')
        collect_mock.assert_not_called()

    @override_settings(PLANTCARE_POLL_BATCH_SIZE=20)
    @patch('jobs.tasks.collect_results', side_effect=OSError('temporary IMAP error'))
    def test_scheduler_keeps_job_waiting_after_retryable_mail_error(
        self,
        _collect_mock,
    ):
        job = AnalysisJob.objects.create(
            analysis_type=AnalysisJob.AnalysisType.CIS_ELEMENTS,
            parameters={'sequence': 'ACGT'},
            status=AnalysisJob.Status.WAITING_EXTERNAL,
            stage='waiting_plantcare',
            provider_ref='pending-ref',
            external_deadline=timezone.now() + timedelta(minutes=30),
        )

        result = poll_waiting_external_jobs()

        job.refresh_from_db()
        self.assertTrue(result['retryable_error'])
        self.assertEqual(job.status, AnalysisJob.Status.WAITING_EXTERNAL)
        self.assertIsNotNone(job.last_polled_at)
        self.assertTrue(
            job.events.filter(event_type='provider_poll_failed').exists()
        )

    @override_settings(PLANTCARE_POLL_BATCH_SIZE=20)
    def test_scheduler_fails_stale_running_lease(self):
        job = AnalysisJob.objects.create(
            analysis_type=AnalysisJob.AnalysisType.CIS_ELEMENTS,
            parameters={'sequence': 'ACGT'},
            status=AnalysisJob.Status.RUNNING,
            stage='submitting',
            lease_expires_at=timezone.now() - timedelta(seconds=1),
        )

        result = poll_waiting_external_jobs()

        job.refresh_from_db()
        self.assertEqual(result['stale_running'], 1)
        self.assertEqual(job.status, AnalysisJob.Status.FAILED)
        self.assertEqual(job.error_code, 'WORKER_LEASE_EXPIRED')

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
from .services import create_analysis_job, create_fasta_input
from .tasks import (
    advance_waiting_workflows,
    execute_analysis_job,
    poll_waiting_external_jobs,
)


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

    def test_completed_job_cannot_be_cancelled(self):
        job = AnalysisJob.objects.create(
            analysis_type=AnalysisJob.AnalysisType.CIS_ELEMENTS,
            parameters={'sequence': 'ACGT'},
            status=AnalysisJob.Status.SUCCEEDED,
            stage='completed',
            progress=100,
        )

        response = self.client.post(f'/api/jobs/{job.id}/cancel')

        self.assertEqual(response.status_code, 409)
        job.refresh_from_db()
        self.assertEqual(job.status, AnalysisJob.Status.SUCCEEDED)

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

    @patch('jobs.services.async_task', return_value='q2-fasta-task')
    def test_upload_fasta_and_create_validation_job(self, _async_task_mock):
        with tempfile.TemporaryDirectory() as temp_dir:
            with override_settings(ARTIFACT_ROOT=Path(temp_dir)):
                input_response = self.client.post(
                    '/api/inputs/fasta',
                    data={
                        'content': '>gene1\nacgt\n',
                        'filename': '../genes.fa',
                    },
                    content_type='application/json',
                )
                self.assertEqual(input_response.status_code, 201)
                input_payload = input_response.json()
                self.assertEqual(input_payload['filename'], 'genes.fa')
                self.assertTrue(
                    (Path(temp_dir) / f"inputs/{input_payload['sha256']}.fasta").is_file()
                )

                job_response = self.client.post(
                    '/api/jobs',
                    data={
                        'analysis_type': 'fasta_validation',
                        'parameters': {
                            'input_artifact_id': input_payload['input_artifact_id'],
                            'alphabet': 'dna',
                        },
                    },
                    content_type='application/json',
                )

        self.assertEqual(job_response.status_code, 202)
        job = AnalysisJob.objects.get(id=job_response.json()['job_id'])
        self.assertEqual(job.analysis_type, AnalysisJob.AnalysisType.FASTA_VALIDATION)
        self.assertNotIn('content', job.parameters)

    def test_fasta_input_is_content_addressed_and_downloadable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with override_settings(ARTIFACT_ROOT=Path(temp_dir)):
                first, first_created = create_fasta_input(
                    '>gene1\nACGT\n',
                    filename='first.fa',
                )
                second, second_created = create_fasta_input(
                    '>gene1\nACGT\n',
                    filename='second.fa',
                )
                response = self.client.get(
                    f'/api/inputs/{first.id}/download'
                )
                body = b''.join(response.streaming_content)

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first.id, second.id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body, b'>gene1\nACGT\n')

    def test_reupload_repairs_missing_content_addressed_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            artifact_root = Path(temp_dir)
            with override_settings(ARTIFACT_ROOT=artifact_root):
                artifact, _ = create_fasta_input('>gene1\nACGT\n')
                stored_path = artifact_root / artifact.storage_path
                stored_path.unlink()

                reused, created = create_fasta_input('>gene1\nACGT\n')

                self.assertFalse(created)
                self.assertEqual(reused.id, artifact.id)
                self.assertEqual(stored_path.read_bytes(), b'>gene1\nACGT\n')

    @override_settings(MAX_FASTA_INPUT_BYTES=5)
    def test_fasta_input_size_limit(self):
        response = self.client.post(
            '/api/inputs/fasta',
            data={'content': '>gene1\nACGT\n'},
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 422)
        self.assertIn('maximum size', response.json()['error']['message'])


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
        self.assertNotIn('timeout', payload)

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

    def test_fasta_worker_normalizes_and_registers_artifacts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            artifact_root = Path(temp_dir)
            with override_settings(ARTIFACT_ROOT=artifact_root):
                input_artifact, _ = create_fasta_input(
                    '>gene1 description\nacgtn\n>gene2\nGGCC\n',
                    filename='genes.fa',
                )
                job = AnalysisJob.objects.create(
                    analysis_type=AnalysisJob.AnalysisType.FASTA_VALIDATION,
                    parameters={
                        'input_artifact_id': str(input_artifact.id),
                        'alphabet': 'auto',
                    },
                )

                result = execute_analysis_job(str(job.id))

                job.refresh_from_db()
                normalized = Artifact.objects.get(job=job, kind='normalized_fasta')
                normalized_path = artifact_root / normalized.storage_path
                self.assertEqual(
                    normalized_path.read_text(encoding='utf-8'),
                    '>gene1 description\nACGTN\n>gene2\nGGCC\n',
                )

        self.assertEqual(result['status'], AnalysisJob.Status.SUCCEEDED)
        self.assertEqual(job.status, AnalysisJob.Status.SUCCEEDED)
        self.assertEqual(job.result['summary']['record_count'], 2)
        self.assertEqual(job.result['summary']['alphabet'], 'dna')
        self.assertEqual(job.result['summary']['gc_percent'], 66.67)
        self.assertEqual(Artifact.objects.filter(job=job).count(), 2)
        self.assertTrue(job.events.filter(event_type='job_succeeded').exists())

    def test_fasta_worker_rejects_duplicate_identifiers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with override_settings(ARTIFACT_ROOT=Path(temp_dir)):
                input_artifact, _ = create_fasta_input(
                    '>duplicate\nACGT\n>duplicate another\nTTTT\n'
                )
                job = AnalysisJob.objects.create(
                    analysis_type=AnalysisJob.AnalysisType.FASTA_VALIDATION,
                    parameters={
                        'input_artifact_id': str(input_artifact.id),
                        'alphabet': 'dna',
                    },
                )

                result = execute_analysis_job(str(job.id))

        job.refresh_from_db()
        self.assertEqual(result['status'], AnalysisJob.Status.FAILED)
        self.assertEqual(job.error_code, 'INVALID_FASTA')
        self.assertIn('identifiers must be unique', job.error_message)

    def test_fasta_worker_detects_tampered_input(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            artifact_root = Path(temp_dir)
            with override_settings(ARTIFACT_ROOT=artifact_root):
                input_artifact, _ = create_fasta_input('>gene1\nACGT\n')
                (artifact_root / input_artifact.storage_path).write_bytes(
                    b'>gene1\nTTTT\n'
                )
                job = AnalysisJob.objects.create(
                    analysis_type=AnalysisJob.AnalysisType.FASTA_VALIDATION,
                    parameters={
                        'input_artifact_id': str(input_artifact.id),
                        'alphabet': 'dna',
                    },
                )

                execute_analysis_job(str(job.id))

        job.refresh_from_db()
        self.assertEqual(job.status, AnalysisJob.Status.FAILED)
        self.assertEqual(job.error_code, 'INVALID_FASTA')
        self.assertIn('checksum', job.error_message)

    def test_alignment_job_uses_mafft_and_preserves_provenance(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            artifact_root = Path(temp_dir)
            with override_settings(
                ARTIFACT_ROOT=artifact_root,
                MAFFT_TIMEOUT=10,
            ):
                input_artifact, _ = create_fasta_input(
                    '>gene1\nACGT\n>gene2\nACGTA\n'
                )
                validation_job = AnalysisJob.objects.create(
                    analysis_type=AnalysisJob.AnalysisType.FASTA_VALIDATION,
                    parameters={
                        'input_artifact_id': str(input_artifact.id),
                        'alphabet': 'dna',
                    },
                )
                execute_analysis_job(str(validation_job.id))
                source = Artifact.objects.get(
                    job=validation_job,
                    kind='normalized_fasta',
                )
                alignment_job, _ = create_analysis_job(
                    AnalysisJob.AnalysisType.MULTIPLE_SEQUENCE_ALIGNMENT,
                    {
                        'artifact_id': str(source.id),
                        'strategy': 'auto',
                        'threads': 1,
                    },
                )
                queued = OrmQ.objects.get()
                payload = SignedPackage.loads(queued.payload)
                def fake_mafft(
                    input_path,
                    output_path,
                    *,
                    strategy,
                    threads,
                    timeout,
                ):
                    self.assertTrue(input_path.is_file())
                    self.assertEqual(strategy, 'auto')
                    self.assertEqual(threads, 1)
                    self.assertEqual(timeout, 10)
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    output_path.write_text(
                        '>gene1\nACGT-\n>gene2\nACGTA\n',
                        encoding='utf-8',
                    )
                    return {
                        'executable': 'fake-mafft',
                        'version': 'v-test',
                        'strategy': strategy,
                        'threads': threads,
                        'stderr_tail': '',
                    }

                with patch('jobs.tasks.run_mafft', side_effect=fake_mafft):
                    result = execute_analysis_job(str(alignment_job.id))

                alignment_job.refresh_from_db()
                aligned = Artifact.objects.get(
                    job=alignment_job,
                    kind='aligned_fasta',
                )
                aligned_text = (
                    artifact_root / aligned.storage_path
                ).read_text(encoding='utf-8')

        self.assertEqual(payload['timeout'], 40)
        self.assertEqual(result['status'], AnalysisJob.Status.SUCCEEDED)
        self.assertEqual(alignment_job.status, AnalysisJob.Status.SUCCEEDED)
        self.assertEqual(alignment_job.result['summary']['alignment_length'], 5)
        self.assertEqual(alignment_job.result['summary']['gap_count'], 1)
        self.assertEqual(aligned_text, '>gene1\nACGT-\n>gene2\nACGTA\n')
        self.assertEqual(aligned.metadata['source_artifact_id'], str(source.id))
        self.assertEqual(aligned.metadata['strategy'], 'auto')
        self.assertEqual(aligned.metadata['tool_version'], 'v-test')

    @override_settings(MAFFT_EXECUTABLE='definitely-missing-mafft')
    def test_alignment_worker_reports_unavailable_runtime(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            artifact_root = Path(temp_dir)
            with override_settings(ARTIFACT_ROOT=artifact_root):
                source_job = AnalysisJob.objects.create(
                    analysis_type=AnalysisJob.AnalysisType.FASTA_VALIDATION,
                    status=AnalysisJob.Status.SUCCEEDED,
                    result={'summary': {'record_count': 2, 'alphabet': 'dna'}},
                )
                source_path = artifact_root / str(source_job.id) / 'normalized.fasta'
                source_path.parent.mkdir(parents=True)
                source_path.write_text('>a\nACGT\n>b\nACGT\n', encoding='utf-8')
                source = Artifact.objects.create(
                    job=source_job,
                    kind='normalized_fasta',
                    filename='normalized.fasta',
                    storage_path=str(source_path.relative_to(artifact_root)),
                    media_type='text/x-fasta',
                    size=source_path.stat().st_size,
                    sha256=hashlib.sha256(source_path.read_bytes()).hexdigest(),
                    metadata={'alphabet': 'dna'},
                )
                job = AnalysisJob.objects.create(
                    analysis_type=(
                        AnalysisJob.AnalysisType.MULTIPLE_SEQUENCE_ALIGNMENT
                    ),
                    parameters={
                        'artifact_id': str(source.id),
                        'strategy': 'auto',
                        'threads': 1,
                    },
                )

                result = execute_analysis_job(str(job.id))

        job.refresh_from_db()
        self.assertEqual(result['status'], AnalysisJob.Status.FAILED)
        self.assertEqual(job.error_code, 'CAPABILITY_UNAVAILABLE')
        self.assertIn('not available', job.error_message)

    def test_phylogenetic_tree_job_creates_validated_newick_artifact(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            artifact_root = Path(temp_dir)
            source_job = AnalysisJob.objects.create(
                analysis_type=AnalysisJob.AnalysisType.MULTIPLE_SEQUENCE_ALIGNMENT,
                status=AnalysisJob.Status.SUCCEEDED,
                result={
                    'summary': {
                        'record_count': 3,
                        'alphabet': 'dna',
                    }
                },
            )
            source_path = artifact_root / str(source_job.id) / 'aligned.fasta'
            source_path.parent.mkdir(parents=True)
            source_path.write_text(
                '>gene1\nACGT\n>gene2\nACGA\n>gene3\nTCGA\n',
                encoding='utf-8',
            )
            source = Artifact.objects.create(
                job=source_job,
                kind='aligned_fasta',
                filename='aligned.fasta',
                storage_path=str(source_path.relative_to(artifact_root)),
                media_type='text/x-fasta',
                size=source_path.stat().st_size,
                sha256=hashlib.sha256(source_path.read_bytes()).hexdigest(),
                metadata={'alphabet': 'dna'},
            )
            with override_settings(
                ARTIFACT_ROOT=artifact_root,
                FASTTREE_TIMEOUT=10,
            ):
                job, _ = create_analysis_job(
                    AnalysisJob.AnalysisType.PHYLOGENETIC_TREE,
                    {
                        'artifact_id': str(source.id),
                        'model': 'auto',
                        'threads': 2,
                    },
                )
                payload = SignedPackage.loads(OrmQ.objects.get().payload)

                def fake_fasttree(
                    input_path,
                    output_path,
                    *,
                    alphabet,
                    model,
                    threads,
                    timeout,
                ):
                    self.assertEqual(input_path, source_path)
                    self.assertEqual(alphabet, 'dna')
                    self.assertEqual(model, 'gtr')
                    self.assertEqual(threads, 2)
                    self.assertEqual(timeout, 10)
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    output_path.write_text(
                        '(gene1:0.1,gene2:0.2,gene3:0.3);\n',
                        encoding='utf-8',
                    )
                    return {
                        'executable': 'fake-fasttree',
                        'version': 'FastTree-test',
                        'alphabet': alphabet,
                        'model': model,
                        'threads': threads,
                    }

                with patch('jobs.tasks.run_fasttree', side_effect=fake_fasttree):
                    result = execute_analysis_job(str(job.id))

                job.refresh_from_db()
                tree = Artifact.objects.get(
                    job=job,
                    kind='phylogenetic_tree_newick',
                )
                tree_text = (artifact_root / tree.storage_path).read_text()

        self.assertEqual(payload['timeout'], 40)
        self.assertEqual(result['status'], AnalysisJob.Status.SUCCEEDED)
        self.assertEqual(job.result['summary']['leaf_count'], 3)
        self.assertEqual(job.result['tool']['model'], 'gtr')
        self.assertEqual(tree.metadata['source_artifact_id'], str(source.id))
        self.assertEqual(tree.metadata['tool_version'], 'FastTree-test')
        self.assertEqual(tree_text, '(gene1:0.1,gene2:0.2,gene3:0.3);\n')

    @override_settings(FASTTREE_EXECUTABLE='definitely-missing-fasttree')
    def test_phylogenetic_tree_worker_reports_unavailable_runtime(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            artifact_root = Path(temp_dir)
            source_job = AnalysisJob.objects.create(
                analysis_type=AnalysisJob.AnalysisType.MULTIPLE_SEQUENCE_ALIGNMENT,
                status=AnalysisJob.Status.SUCCEEDED,
                result={'summary': {'record_count': 3, 'alphabet': 'dna'}},
            )
            source_path = artifact_root / str(source_job.id) / 'aligned.fasta'
            source_path.parent.mkdir(parents=True)
            source_path.write_text(
                '>a\nACGT\n>b\nACGA\n>c\nTCGA\n',
                encoding='utf-8',
            )
            source = Artifact.objects.create(
                job=source_job,
                kind='aligned_fasta',
                filename='aligned.fasta',
                storage_path=str(source_path.relative_to(artifact_root)),
                media_type='text/x-fasta',
                size=source_path.stat().st_size,
                sha256=hashlib.sha256(source_path.read_bytes()).hexdigest(),
                metadata={'alphabet': 'dna'},
            )
            with override_settings(ARTIFACT_ROOT=artifact_root):
                job = AnalysisJob.objects.create(
                    analysis_type=AnalysisJob.AnalysisType.PHYLOGENETIC_TREE,
                    parameters={
                        'artifact_id': str(source.id),
                        'model': 'gtr',
                        'threads': 1,
                    },
                )
                result = execute_analysis_job(str(job.id))

        job.refresh_from_db()
        self.assertEqual(result['status'], AnalysisJob.Status.FAILED)
        self.assertEqual(job.error_code, 'CAPABILITY_UNAVAILABLE')

    @patch('jobs.services.async_task', side_effect=['validation-q', 'alignment-q', 'tree-q'])
    def test_sequence_phylogeny_workflow_advances_and_aggregates_result(
        self,
        async_task_mock,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            artifact_root = Path(temp_dir)
            with override_settings(ARTIFACT_ROOT=artifact_root):
                input_artifact, _ = create_fasta_input(
                    '>gene1\nACGT\n>gene2\nACGA\n>gene3\nTCGA\n'
                )
                parent, created = create_analysis_job(
                    AnalysisJob.AnalysisType.SEQUENCE_PHYLOGENY,
                    {
                        'input_artifact_id': str(input_artifact.id),
                        'alphabet': 'auto',
                        'alignment_strategy': 'linsi',
                        'tree_model': 'gtr',
                        'threads': 2,
                    },
                )
                validation = parent.child_jobs.get(workflow_step='validation')
                validation.status = AnalysisJob.Status.SUCCEEDED
                validation.result = {
                    'summary': {'record_count': 3, 'alphabet': 'dna'}
                }
                validation.save(update_fields=['status', 'result', 'updated_at'])
                normalized_path = artifact_root / str(validation.id) / 'normalized.fasta'
                normalized_path.parent.mkdir(parents=True)
                normalized_path.write_text(
                    '>gene1\nACGT\n>gene2\nACGA\n>gene3\nTCGA\n',
                    encoding='utf-8',
                )
                normalized = Artifact.objects.create(
                    job=validation,
                    kind='normalized_fasta',
                    filename='normalized.fasta',
                    storage_path=str(normalized_path.relative_to(artifact_root)),
                    media_type='text/x-fasta',
                    size=normalized_path.stat().st_size,
                    sha256=hashlib.sha256(normalized_path.read_bytes()).hexdigest(),
                    metadata={'alphabet': 'dna'},
                )

                first_advance = advance_waiting_workflows()
                alignment = parent.child_jobs.get(workflow_step='alignment')
                self.assertEqual(
                    alignment.parameters,
                    {
                        'artifact_id': str(normalized.id),
                        'strategy': 'linsi',
                        'threads': 2,
                    },
                )
                alignment.status = AnalysisJob.Status.SUCCEEDED
                alignment.result = {
                    'summary': {
                        'record_count': 3,
                        'alphabet': 'dna',
                        'alignment_length': 4,
                    }
                }
                alignment.save(update_fields=['status', 'result', 'updated_at'])
                aligned_path = artifact_root / str(alignment.id) / 'aligned.fasta'
                aligned_path.parent.mkdir(parents=True)
                aligned_path.write_text(
                    '>gene1\nACGT\n>gene2\nACGA\n>gene3\nTCGA\n',
                    encoding='utf-8',
                )
                aligned = Artifact.objects.create(
                    job=alignment,
                    kind='aligned_fasta',
                    filename='aligned.fasta',
                    storage_path=str(aligned_path.relative_to(artifact_root)),
                    media_type='text/x-fasta',
                    size=aligned_path.stat().st_size,
                    sha256=hashlib.sha256(aligned_path.read_bytes()).hexdigest(),
                    metadata={'alphabet': 'dna'},
                )

                second_advance = advance_waiting_workflows()
                tree = parent.child_jobs.get(workflow_step='tree')
                self.assertEqual(tree.parameters['artifact_id'], str(aligned.id))
                tree.status = AnalysisJob.Status.SUCCEEDED
                tree.result = {'summary': {'leaf_count': 3}}
                tree.save(update_fields=['status', 'result', 'updated_at'])
                tree_path = artifact_root / str(tree.id) / 'tree.newick'
                tree_path.parent.mkdir(parents=True)
                tree_path.write_text(
                    '(gene1:0.1,gene2:0.2,gene3:0.3);\n',
                    encoding='utf-8',
                )
                tree_artifact = Artifact.objects.create(
                    job=tree,
                    kind='phylogenetic_tree_newick',
                    filename='tree.newick',
                    storage_path=str(tree_path.relative_to(artifact_root)),
                    media_type='text/x-newick',
                    size=tree_path.stat().st_size,
                    sha256=hashlib.sha256(tree_path.read_bytes()).hexdigest(),
                )

                third_advance = advance_waiting_workflows()
                parent.refresh_from_db()
                result_response = self.client.get(
                    f'/api/jobs/{parent.id}/result'
                )

        self.assertTrue(created)
        self.assertEqual(first_advance['checked'], 1)
        self.assertEqual(second_advance['checked'], 1)
        self.assertEqual(third_advance['completed'], 1)
        self.assertEqual(parent.status, AnalysisJob.Status.SUCCEEDED)
        self.assertEqual(parent.result['summary']['leaf_count'], 3)
        self.assertEqual(
            parent.result['final_artifact']['artifact_id'],
            str(tree_artifact.id),
        )
        self.assertEqual(result_response.status_code, 200)
        self.assertEqual(len(result_response.json()['artifacts']), 3)
        self.assertEqual(async_task_mock.call_count, 3)

    @patch('jobs.services.async_task', return_value='validation-q')
    def test_sequence_phylogeny_propagates_child_failure(self, _async_task_mock):
        with tempfile.TemporaryDirectory() as temp_dir:
            with override_settings(ARTIFACT_ROOT=Path(temp_dir)):
                input_artifact, _ = create_fasta_input('>a\nACGT\n>b\nACGA\n>c\nTCGA\n')
                parent, _ = create_analysis_job(
                    AnalysisJob.AnalysisType.SEQUENCE_PHYLOGENY,
                    {
                        'input_artifact_id': str(input_artifact.id),
                        'alphabet': 'dna',
                        'alignment_strategy': 'auto',
                        'tree_model': 'auto',
                        'threads': 1,
                    },
                )
                validation = parent.child_jobs.get(workflow_step='validation')
                validation.status = AnalysisJob.Status.FAILED
                validation.error_code = 'INVALID_FASTA'
                validation.save(
                    update_fields=['status', 'error_code', 'updated_at']
                )

                advance_waiting_workflows()

        parent.refresh_from_db()
        self.assertEqual(parent.status, AnalysisJob.Status.FAILED)
        self.assertEqual(parent.error_code, 'WORKFLOW_STEP_FAILED')
        self.assertIn('INVALID_FASTA', parent.error_message)

    @patch('jobs.services.async_task', return_value='validation-q')
    def test_cancelling_workflow_cancels_active_child(self, _async_task_mock):
        with tempfile.TemporaryDirectory() as temp_dir:
            with override_settings(ARTIFACT_ROOT=Path(temp_dir)):
                input_artifact, _ = create_fasta_input('>a\nACGT\n>b\nACGA\n>c\nTCGA\n')
                parent, _ = create_analysis_job(
                    AnalysisJob.AnalysisType.SEQUENCE_PHYLOGENY,
                    {
                        'input_artifact_id': str(input_artifact.id),
                        'alphabet': 'dna',
                        'alignment_strategy': 'auto',
                        'tree_model': 'auto',
                        'threads': 1,
                    },
                )
                child = parent.child_jobs.get(workflow_step='validation')

                response = self.client.post(f'/api/jobs/{parent.id}/cancel')

        parent.refresh_from_db()
        child.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(parent.status, AnalysisJob.Status.CANCELLED)
        self.assertEqual(child.status, AnalysisJob.Status.CANCELLED)

    def test_workflow_schedule_is_installed_by_migration(self):
        schedule = Schedule.objects.get(name='gene-family-advance-workflows')
        self.assertEqual(schedule.func, 'jobs.tasks.advance_waiting_workflows')
        self.assertEqual(schedule.schedule_type, Schedule.MINUTES)
        self.assertEqual(schedule.minutes, 1)
        self.assertEqual(schedule.repeats, -1)

    @patch('jobs.services.async_task', return_value='validation-q')
    def test_sequence_phylogeny_idempotency_reuses_parent_and_child(
        self,
        async_task_mock,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            with override_settings(ARTIFACT_ROOT=Path(temp_dir)):
                input_artifact, _ = create_fasta_input(
                    '>a\nACGT\n>b\nACGA\n>c\nTCGA\n'
                )
                parameters = {
                    'input_artifact_id': str(input_artifact.id),
                    'alphabet': 'dna',
                    'alignment_strategy': 'auto',
                    'tree_model': 'auto',
                    'threads': 1,
                }
                first, first_created = create_analysis_job(
                    AnalysisJob.AnalysisType.SEQUENCE_PHYLOGENY,
                    parameters,
                    idempotency_key='workflow-idempotency',
                )
                second, second_created = create_analysis_job(
                    AnalysisJob.AnalysisType.SEQUENCE_PHYLOGENY,
                    parameters,
                    idempotency_key='workflow-idempotency',
                )
                status_response = self.client.get(f'/api/jobs/{first.id}')

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first.id, second.id)
        self.assertEqual(first.child_jobs.count(), 1)
        self.assertEqual(async_task_mock.call_count, 1)
        self.assertEqual(
            status_response.json()['workflow_steps'][0]['step'],
            'validation',
        )

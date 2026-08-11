from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from cis_elements.services import run_prediction

from .models import AnalysisJob, Artifact
from .services import add_event, artifact_payload


def _set_state(job: AnalysisJob, status: str, stage: str, progress: int | None):
    if job.status == AnalysisJob.Status.CANCELLED:
        return False
    job.status = status
    job.stage = stage
    job.progress = progress
    fields = ['status', 'stage', 'progress', 'updated_at']
    if status == AnalysisJob.Status.RUNNING and job.started_at is None:
        job.started_at = timezone.now()
        fields.append('started_at')
    job.save(update_fields=fields)
    add_event(job, 'status_changed', f'Job entered {status}/{stage}')
    return True


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _register_artifact(job: AnalysisJob, path: Path) -> Artifact:
    artifact_root = Path(settings.ARTIFACT_ROOT).resolve()
    resolved_path = path.resolve()
    if not resolved_path.is_relative_to(artifact_root):
        raise ValueError('artifact path is outside ARTIFACT_ROOT')
    return Artifact.objects.create(
        job=job,
        kind='plantcare_attachment',
        filename=resolved_path.name,
        storage_path=str(resolved_path.relative_to(artifact_root)),
        media_type=mimetypes.guess_type(resolved_path.name)[0]
        or 'application/octet-stream',
        size=resolved_path.stat().st_size,
        sha256=_sha256(resolved_path),
    )


def _public_result(provider_result: dict, artifacts: list[Artifact]) -> dict:
    return {
        'ref': provider_result.get('ref', ''),
        'subject': provider_result.get('subject', ''),
        'date': provider_result.get('date', ''),
        'artifacts': [artifact_payload(artifact) for artifact in artifacts],
    }


def _error_details(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, TimeoutError):
        return 'PROVIDER_TIMEOUT', 'PlantCARE result collection timed out'
    if isinstance(exc, ValueError):
        return 'CAPABILITY_UNAVAILABLE', str(exc)
    return 'PROVIDER_EXECUTION_FAILED', 'PlantCARE analysis failed'


def execute_analysis_job(job_id: str) -> dict:
    try:
        job = AnalysisJob.objects.get(id=job_id)
    except AnalysisJob.DoesNotExist:
        return {'job_id': job_id, 'status': 'not_found'}

    if job.status == AnalysisJob.Status.CANCELLED:
        return {'job_id': job_id, 'status': job.status}

    try:
        if not _set_state(job, AnalysisJob.Status.RUNNING, 'submitting', 10):
            return {'job_id': job_id, 'status': job.status}

        output_dir = Path(settings.ARTIFACT_ROOT) / str(job.id)

        def on_submitted(ref: str):
            job.refresh_from_db()
            if _set_state(
                job,
                AnalysisJob.Status.WAITING_EXTERNAL,
                'waiting_plantcare',
                40,
            ):
                add_event(
                    job,
                    'provider_submitted',
                    'PlantCARE request submitted',
                    {'provider_ref': ref},
                )

        def on_result_received():
            job.refresh_from_db()
            _set_state(job, AnalysisJob.Status.RUNNING, 'collecting_result', 80)

        provider_result = run_prediction(
            job.parameters['sequence'],
            output_dir=output_dir,
            on_submitted=on_submitted,
            on_result_received=on_result_received,
        )
        job.refresh_from_db()
        if job.status == AnalysisJob.Status.CANCELLED:
            return {'job_id': job_id, 'status': job.status}

        artifacts = [
            _register_artifact(job, Path(path))
            for path in provider_result.get('attachments', [])
        ]
        job.result = _public_result(provider_result, artifacts)
        job.status = AnalysisJob.Status.SUCCEEDED
        job.stage = 'completed'
        job.progress = 100
        job.finished_at = timezone.now()
        job.error_code = ''
        job.error_message = ''
        job.save(
            update_fields=[
                'result',
                'status',
                'stage',
                'progress',
                'finished_at',
                'error_code',
                'error_message',
                'updated_at',
            ]
        )
        add_event(job, 'job_succeeded', 'Analysis job completed')
        return {'job_id': job_id, 'status': job.status}
    except Exception as exc:
        job.refresh_from_db()
        if job.status == AnalysisJob.Status.CANCELLED:
            return {'job_id': job_id, 'status': job.status}
        error_code, error_message = _error_details(exc)
        job.status = AnalysisJob.Status.FAILED
        job.stage = 'failed'
        job.progress = None
        job.error_code = error_code
        job.error_message = error_message
        job.finished_at = timezone.now()
        job.save(
            update_fields=[
                'status',
                'stage',
                'progress',
                'error_code',
                'error_message',
                'finished_at',
                'updated_at',
            ]
        )
        add_event(
            job,
            'job_failed',
            error_message,
            {'exception_type': type(exc).__name__},
        )
        return {
            'job_id': job_id,
            'status': job.status,
            'error_code': error_code,
        }

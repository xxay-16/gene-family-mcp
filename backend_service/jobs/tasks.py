from __future__ import annotations

import hashlib
import mimetypes
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from cis_elements.services import collect_results, submit_prediction
from cis_elements.parser import process_plantcare_attachments

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


def _artifact_kind(path: Path, structured_path: Path | None = None) -> str:
    if structured_path is not None and path.resolve() == structured_path.resolve():
        return 'plantcare_structured_result'
    if path.suffix.lower() == '.tab':
        return 'plantcare_table'
    if path.suffix.lower() in {'.html', '.htm'}:
        return 'plantcare_report'
    if path.name.lower().endswith(('.tar.gz', '.tgz', '.tar')):
        return 'plantcare_archive'
    return 'plantcare_attachment'


def _register_artifact(
    job: AnalysisJob,
    path: Path,
    *,
    structured_path: Path | None = None,
) -> Artifact:
    artifact_root = Path(settings.ARTIFACT_ROOT).resolve()
    resolved_path = path.resolve()
    if not resolved_path.is_relative_to(artifact_root):
        raise ValueError('artifact path is outside ARTIFACT_ROOT')
    storage_path = str(resolved_path.relative_to(artifact_root))
    defaults = {
        'kind': _artifact_kind(resolved_path, structured_path),
        'filename': resolved_path.name,
        'media_type': mimetypes.guess_type(resolved_path.name)[0]
        or 'application/octet-stream',
        'size': resolved_path.stat().st_size,
        'sha256': _sha256(resolved_path),
    }
    artifact, _ = Artifact.objects.update_or_create(
        job=job,
        storage_path=storage_path,
        defaults=defaults,
    )
    return artifact


def _public_result(
    provider_result: dict,
    artifacts: list[Artifact],
    summary: dict,
) -> dict:
    return {
        'ref': provider_result.get('ref', ''),
        'subject': provider_result.get('subject', ''),
        'date': provider_result.get('date', ''),
        'summary': summary,
        'artifacts': [artifact_payload(artifact) for artifact in artifacts],
    }


def _error_details(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, TimeoutError):
        return 'PROVIDER_TIMEOUT', 'PlantCARE result collection timed out'
    if isinstance(exc, ValueError):
        return 'CAPABILITY_UNAVAILABLE', str(exc)
    return 'PROVIDER_EXECUTION_FAILED', 'PlantCARE analysis failed'


def _fail_job(job: AnalysisJob, error_code: str, error_message: str, exc=None):
    if job.status == AnalysisJob.Status.CANCELLED:
        return
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
    details = {'exception_type': type(exc).__name__} if exc is not None else {}
    add_event(job, 'job_failed', error_message, details)


def _complete_job(job: AnalysisJob, provider_result: dict):
    output_files = [Path(path) for path in provider_result.get('attachments', [])]
    output_dir = Path(settings.ARTIFACT_ROOT) / str(job.id)
    processed = process_plantcare_attachments(
        output_files,
        output_dir,
        max_members=settings.PLANTCARE_ARCHIVE_MAX_MEMBERS,
        max_file_size=settings.PLANTCARE_ARCHIVE_MAX_FILE_SIZE,
        max_total_size=settings.PLANTCARE_ARCHIVE_MAX_TOTAL_SIZE,
    )
    all_files = []
    seen = set()
    for path in [
        *output_files,
        *processed['derived_files'],
        processed['structured_path'],
    ]:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        all_files.append(path)
    artifacts = [
        _register_artifact(
            job,
            path,
            structured_path=processed['structured_path'],
        )
        for path in all_files
    ]
    job.result = _public_result(
        provider_result,
        artifacts,
        processed['summary'],
    )
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


def execute_analysis_job(job_id: str) -> dict:
    """Submit an analysis to its provider, then release the worker."""
    try:
        job = AnalysisJob.objects.get(id=job_id)
    except AnalysisJob.DoesNotExist:
        return {'job_id': job_id, 'status': 'not_found'}

    if job.status == AnalysisJob.Status.CANCELLED:
        return {'job_id': job_id, 'status': job.status}

    try:
        if not _set_state(job, AnalysisJob.Status.RUNNING, 'submitting', 10):
            return {'job_id': job_id, 'status': job.status}
        provider_ref = submit_prediction(job.parameters['sequence'])
        job.refresh_from_db()
        if job.status == AnalysisJob.Status.CANCELLED:
            return {'job_id': job_id, 'status': job.status}

        job.provider_ref = provider_ref
        job.external_deadline = timezone.now() + timedelta(
            seconds=settings.PLANTCARE_RESULT_TIMEOUT
        )
        job.status = AnalysisJob.Status.WAITING_EXTERNAL
        job.stage = 'waiting_plantcare'
        job.progress = 40
        job.save(
            update_fields=[
                'provider_ref',
                'external_deadline',
                'status',
                'stage',
                'progress',
                'updated_at',
            ]
        )
        add_event(
            job,
            'provider_submitted',
            'PlantCARE request submitted',
            {'provider_ref': provider_ref},
        )
        return {'job_id': job_id, 'status': job.status}
    except Exception as exc:
        job.refresh_from_db()
        error_code, error_message = _error_details(exc)
        _fail_job(job, error_code, error_message, exc)
        return {
            'job_id': job_id,
            'status': job.status,
            'error_code': error_code,
        }


def poll_waiting_external_jobs() -> dict:
    """Collect available PlantCARE mail once for a bounded job batch."""
    now = timezone.now()
    expired_jobs = list(
        AnalysisJob.objects.filter(
            status=AnalysisJob.Status.WAITING_EXTERNAL,
            external_deadline__lte=now,
        )[: settings.PLANTCARE_POLL_BATCH_SIZE]
    )
    for job in expired_jobs:
        _fail_job(job, 'PROVIDER_TIMEOUT', 'PlantCARE result collection timed out')

    remaining_capacity = max(
        settings.PLANTCARE_POLL_BATCH_SIZE - len(expired_jobs),
        0,
    )
    jobs = list(
        AnalysisJob.objects.filter(
            status=AnalysisJob.Status.WAITING_EXTERNAL,
            external_deadline__gt=now,
        )
        .exclude(provider_ref='')
        .order_by('last_polled_at', 'created_at')[:remaining_capacity]
    )
    if not jobs:
        return {
            'checked': 0,
            'completed': 0,
            'expired': len(expired_jobs),
        }

    ref_to_output_dir = {
        job.provider_ref: Path(settings.ARTIFACT_ROOT) / str(job.id) for job in jobs
    }
    try:
        results = collect_results(ref_to_output_dir)
    except ValueError as exc:
        for job in jobs:
            _fail_job(job, 'CAPABILITY_UNAVAILABLE', str(exc), exc)
        return {
            'checked': len(jobs),
            'completed': 0,
            'expired': len(expired_jobs),
            'failed': len(jobs),
        }
    except Exception as exc:
        for job in jobs:
            job.last_polled_at = now
            job.save(update_fields=['last_polled_at', 'updated_at'])
            add_event(
                job,
                'provider_poll_failed',
                'PlantCARE result check failed; the scheduler will retry',
                {'exception_type': type(exc).__name__},
            )
        return {
            'checked': len(jobs),
            'completed': 0,
            'expired': len(expired_jobs),
            'retryable_error': True,
        }

    completed = 0
    for job in jobs:
        job.last_polled_at = now
        job.save(update_fields=['last_polled_at', 'updated_at'])
        provider_result = results.get(job.provider_ref)
        if provider_result is None:
            continue
        job.refresh_from_db()
        if job.status != AnalysisJob.Status.WAITING_EXTERNAL:
            continue
        _set_state(job, AnalysisJob.Status.RUNNING, 'collecting_result', 80)
        try:
            _complete_job(job, provider_result)
            completed += 1
        except Exception as exc:
            _fail_job(
                job,
                'RESULT_PROCESSING_FAILED',
                'PlantCARE result processing failed',
                exc,
            )

    return {
        'checked': len(jobs),
        'completed': completed,
        'expired': len(expired_jobs),
    }

from __future__ import annotations

from typing import Any

from django.db import transaction
from django_q.models import OrmQ
from django_q.signing import SignedPackage
from django_q.tasks import async_task

from .models import AnalysisEvent, AnalysisJob, Artifact


TERMINAL_STATUSES = {
    AnalysisJob.Status.SUCCEEDED,
    AnalysisJob.Status.FAILED,
    AnalysisJob.Status.CANCELLED,
}


def normalize_dna_sequence(value: str) -> str:
    sequence = ''.join(value.split()).upper()
    if not sequence:
        raise ValueError('sequence must not be empty')
    if any(base not in 'ACGTN' for base in sequence):
        raise ValueError('sequence must contain only A, C, G, T or N')
    return sequence


def add_event(
    job: AnalysisJob,
    event_type: str,
    message: str = '',
    details: dict[str, Any] | None = None,
) -> AnalysisEvent:
    return AnalysisEvent.objects.create(
        job=job,
        event_type=event_type,
        message=message,
        details=details or {},
    )


def create_analysis_job(analysis_type: str, parameters: dict[str, Any]) -> AnalysisJob:
    if analysis_type != AnalysisJob.AnalysisType.CIS_ELEMENTS:
        raise ValueError(f'unsupported analysis type: {analysis_type}')

    normalized_parameters = dict(parameters)
    normalized_parameters['sequence'] = normalize_dna_sequence(
        str(parameters.get('sequence', ''))
    )

    with transaction.atomic():
        job = AnalysisJob.objects.create(
            analysis_type=analysis_type,
            parameters=normalized_parameters,
        )
        add_event(job, 'job_created', 'Analysis job created')

    try:
        queue_task_id = async_task(
            'jobs.tasks.execute_analysis_job',
            str(job.id),
            task_name=f'analysis-job-{job.id}',
            save=False,
        )
    except Exception as exc:
        job.status = AnalysisJob.Status.FAILED
        job.stage = 'enqueue'
        job.error_code = 'QUEUE_UNAVAILABLE'
        job.error_message = 'Unable to enqueue analysis job'
        job.save(
            update_fields=[
                'status',
                'stage',
                'error_code',
                'error_message',
                'updated_at',
            ]
        )
        add_event(
            job,
            'enqueue_failed',
            job.error_message,
            {'exception_type': type(exc).__name__},
        )
        raise RuntimeError(job.error_message) from exc

    job.queue_task_id = queue_task_id
    job.save(update_fields=['queue_task_id', 'updated_at'])
    add_event(job, 'job_queued', 'Analysis job queued with django-q2')
    return job


def _delete_queued_task(queue_task_id: str) -> bool:
    if not queue_task_id:
        return False
    for queued_task in OrmQ.objects.all():
        try:
            payload = SignedPackage.loads(queued_task.payload)
        except Exception:
            continue
        if payload.get('id') == queue_task_id:
            queued_task.delete()
            return True
    return False


def cancel_analysis_job(job: AnalysisJob) -> AnalysisJob:
    if job.status in TERMINAL_STATUSES:
        raise ValueError(f'job is already {job.status}')

    previous_status = job.status
    job.status = AnalysisJob.Status.CANCELLED
    job.stage = 'cancelled'
    job.progress = None
    from django.utils import timezone

    job.finished_at = timezone.now()
    job.save(
        update_fields=['status', 'stage', 'progress', 'finished_at', 'updated_at']
    )
    removed_from_queue = _delete_queued_task(job.queue_task_id)
    add_event(
        job,
        'job_cancelled',
        'Analysis job cancelled',
        {
            'previous_status': previous_status,
            'removed_from_queue': removed_from_queue,
        },
    )
    return job


def artifact_payload(artifact: Artifact) -> dict[str, Any]:
    return {
        'artifact_id': str(artifact.id),
        'kind': artifact.kind,
        'filename': artifact.filename,
        'media_type': artifact.media_type,
        'size': artifact.size,
        'sha256': artifact.sha256,
        'metadata': artifact.metadata,
        'download_url': f'/api/artifacts/{artifact.id}/download',
    }


def job_payload(job: AnalysisJob, include_result: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        'job_id': str(job.id),
        'analysis_type': job.analysis_type,
        'status': job.status,
        'stage': job.stage,
        'progress': job.progress,
        'created_at': job.created_at.isoformat(),
        'started_at': job.started_at.isoformat() if job.started_at else None,
        'finished_at': job.finished_at.isoformat() if job.finished_at else None,
        'status_url': f'/api/jobs/{job.id}',
        'result_url': f'/api/jobs/{job.id}/result',
    }
    if job.error_code:
        payload['error'] = {
            'code': job.error_code,
            'message': job.error_message,
        }
    if include_result:
        payload['result'] = job.result
        payload['artifacts'] = [
            artifact_payload(artifact) for artifact in job.artifacts.all()
        ]
    return payload

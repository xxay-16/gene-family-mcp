from uuid import UUID

from django.http import FileResponse
from django.shortcuts import get_object_or_404
from ninja import Router, Schema
from ninja.responses import Response

from .models import AnalysisJob, Artifact, InputArtifact
from .services import (
    IdempotencyConflictError,
    JobCapacityError,
    cancel_analysis_job,
    create_analysis_job,
    create_fasta_input,
    input_artifact_payload,
    job_payload,
)

router = Router(tags=['jobs'])
artifact_router = Router(tags=['artifacts'])
input_router = Router(tags=['inputs'])


class JobCreateIn(Schema):
    analysis_type: str
    parameters: dict


class FastaInputCreateIn(Schema):
    content: str
    filename: str = 'input.fasta'


def _error(code: str, message: str, status: int):
    return Response({'error': {'code': code, 'message': message}}, status=status)


@input_router.post('/fasta', response={200: dict, 201: dict})
def create_fasta_input_artifact(request, payload: FastaInputCreateIn):
    try:
        artifact, created = create_fasta_input(
            payload.content,
            filename=payload.filename,
        )
    except ValueError as exc:
        return _error('INVALID_INPUT', str(exc), 422)
    response = input_artifact_payload(artifact)
    response['created'] = created
    return (201 if created else 200), response


@router.post('', response={202: dict})
def create_job(request, payload: JobCreateIn):
    try:
        job, created = create_analysis_job(
            payload.analysis_type,
            payload.parameters,
            idempotency_key=request.headers.get('Idempotency-Key', ''),
        )
    except IdempotencyConflictError as exc:
        return _error('IDEMPOTENCY_CONFLICT', str(exc), 409)
    except ValueError as exc:
        return _error('INVALID_INPUT', str(exc), 422)
    except JobCapacityError as exc:
        return _error('JOB_CAPACITY_REACHED', str(exc), 503)
    except RuntimeError as exc:
        return _error('QUEUE_UNAVAILABLE', str(exc), 503)
    response = job_payload(job)
    response['created'] = created
    return 202, response


@router.get('')
def list_jobs(request, status: str | None = None, limit: int = 50):
    limit = min(max(limit, 1), 200)
    jobs = AnalysisJob.objects.all()
    if status:
        if status not in AnalysisJob.Status.values:
            return _error('INVALID_STATUS', f'Unknown job status: {status}', 422)
        jobs = jobs.filter(status=status)
    return {
        'jobs': [job_payload(job) for job in jobs[:limit]],
        'limit': limit,
    }


@router.get('/{job_id}')
def get_job(request, job_id: UUID):
    try:
        job = AnalysisJob.objects.get(id=job_id)
    except AnalysisJob.DoesNotExist:
        return _error('JOB_NOT_FOUND', 'Analysis job not found', 404)
    return job_payload(job)


@router.get('/{job_id}/events')
def get_job_events(request, job_id: UUID):
    try:
        job = AnalysisJob.objects.get(id=job_id)
    except AnalysisJob.DoesNotExist:
        return _error('JOB_NOT_FOUND', 'Analysis job not found', 404)
    return {
        'job_id': str(job.id),
        'events': [
            {
                'event_type': event.event_type,
                'message': event.message,
                'details': event.details,
                'created_at': event.created_at.isoformat(),
            }
            for event in job.events.all()
        ],
    }


@router.get('/{job_id}/result')
def get_job_result(request, job_id: UUID):
    try:
        job = AnalysisJob.objects.prefetch_related('artifacts').get(id=job_id)
    except AnalysisJob.DoesNotExist:
        return _error('JOB_NOT_FOUND', 'Analysis job not found', 404)
    if job.status != AnalysisJob.Status.SUCCEEDED:
        return _error(
            'JOB_NOT_COMPLETE',
            f'Analysis job is {job.status}',
            409,
        )
    return job_payload(job, include_result=True)


@router.post('/{job_id}/cancel')
def cancel_job(request, job_id: UUID):
    try:
        job = AnalysisJob.objects.get(id=job_id)
    except AnalysisJob.DoesNotExist:
        return _error('JOB_NOT_FOUND', 'Analysis job not found', 404)
    try:
        job = cancel_analysis_job(job)
    except ValueError as exc:
        return _error('JOB_NOT_CANCELLABLE', str(exc), 409)
    return job_payload(job)


@artifact_router.get('/{artifact_id}/download')
def download_artifact(request, artifact_id: UUID):
    artifact = get_object_or_404(Artifact, id=artifact_id)
    from pathlib import Path

    from django.conf import settings

    artifact_root = Path(settings.ARTIFACT_ROOT).resolve()
    file_path = (artifact_root / artifact.storage_path).resolve()
    if not file_path.is_relative_to(artifact_root) or not file_path.is_file():
        return _error('ARTIFACT_NOT_FOUND', 'Artifact file not found', 404)
    return FileResponse(
        file_path.open('rb'),
        as_attachment=True,
        filename=artifact.filename,
        content_type=artifact.media_type,
    )


@input_router.get('/{input_artifact_id}/download')
def download_input_artifact(request, input_artifact_id: UUID):
    try:
        artifact = InputArtifact.objects.get(id=input_artifact_id)
    except InputArtifact.DoesNotExist:
        return _error('INPUT_ARTIFACT_NOT_FOUND', 'Input artifact not found', 404)
    from pathlib import Path

    from django.conf import settings

    artifact_root = Path(settings.ARTIFACT_ROOT).resolve()
    file_path = (artifact_root / artifact.storage_path).resolve()
    if not file_path.is_relative_to(artifact_root) or not file_path.is_file():
        return _error('INPUT_ARTIFACT_NOT_FOUND', 'Input artifact file not found', 404)
    return FileResponse(
        file_path.open('rb'),
        as_attachment=True,
        filename=artifact.filename,
        content_type=artifact.media_type,
    )

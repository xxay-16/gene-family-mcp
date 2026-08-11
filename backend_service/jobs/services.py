from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django_q.models import OrmQ
from django_q.signing import SignedPackage
from django_q.tasks import async_task

from .models import AnalysisEvent, AnalysisJob, Artifact, InputArtifact

TERMINAL_STATUSES = {
    AnalysisJob.Status.SUCCEEDED,
    AnalysisJob.Status.FAILED,
    AnalysisJob.Status.CANCELLED,
}


class IdempotencyConflictError(ValueError):
    pass


class JobCapacityError(RuntimeError):
    pass


def normalize_dna_sequence(value: str) -> str:
    sequence = ''.join(value.split()).upper()
    if not sequence:
        raise ValueError('sequence must not be empty')
    if any(base not in 'ACGTN' for base in sequence):
        raise ValueError('sequence must contain only A, C, G, T or N')
    if len(sequence) > settings.MAX_SEQUENCE_LENGTH:
        raise ValueError(
            f'sequence exceeds maximum length of {settings.MAX_SEQUENCE_LENGTH}'
        )
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


def create_analysis_job(
    analysis_type: str,
    parameters: dict[str, Any],
    *,
    idempotency_key: str = '',
) -> tuple[AnalysisJob, bool]:
    if analysis_type not in AnalysisJob.AnalysisType.values:
        raise ValueError(f'unsupported analysis type: {analysis_type}')

    normalized_parameters = dict(parameters)
    if analysis_type == AnalysisJob.AnalysisType.CIS_ELEMENTS:
        normalized_parameters = {
            'sequence': normalize_dna_sequence(str(parameters.get('sequence', '')))
        }
    elif analysis_type == AnalysisJob.AnalysisType.FASTA_VALIDATION:
        input_artifact_id = str(parameters.get('input_artifact_id', '')).strip()
        try:
            parsed_artifact_id = uuid.UUID(input_artifact_id)
        except ValueError as exc:
            raise ValueError('input_artifact_id must be a valid UUID') from exc
        input_artifact = InputArtifact.objects.filter(
            id=parsed_artifact_id,
            kind='fasta_input',
        ).first()
        if input_artifact is None:
            raise ValueError('FASTA input artifact was not found')
        alphabet = str(parameters.get('alphabet', 'auto')).strip().lower()
        if alphabet not in {'auto', 'dna', 'protein'}:
            raise ValueError('alphabet must be one of: auto, dna, protein')
        normalized_parameters = {
            'input_artifact_id': str(input_artifact.id),
            'alphabet': alphabet,
        }

    normalized_idempotency_key = idempotency_key.strip()
    if len(normalized_idempotency_key) > 128:
        raise ValueError('idempotency key must be at most 128 characters')
    if normalized_idempotency_key:
        existing_job = AnalysisJob.objects.filter(
            analysis_type=analysis_type,
            idempotency_key=normalized_idempotency_key,
        ).first()
        if existing_job is not None:
            if existing_job.parameters != normalized_parameters:
                raise IdempotencyConflictError(
                    'idempotency key was already used with different parameters'
                )
            return existing_job, False

    active_count = AnalysisJob.objects.exclude(status__in=TERMINAL_STATUSES).count()
    if active_count >= settings.MAX_ACTIVE_JOBS:
        raise JobCapacityError('active analysis job capacity has been reached')

    try:
        with transaction.atomic():
            job = AnalysisJob.objects.create(
                analysis_type=analysis_type,
                parameters=normalized_parameters,
                idempotency_key=normalized_idempotency_key,
            )
            add_event(job, 'job_created', 'Analysis job created')
    except IntegrityError:
        if not normalized_idempotency_key:
            raise
        existing_job = AnalysisJob.objects.get(
            analysis_type=analysis_type,
            idempotency_key=normalized_idempotency_key,
        )
        if existing_job.parameters != normalized_parameters:
            raise IdempotencyConflictError(
                'idempotency key was already used with different parameters'
            ) from None
        return existing_job, False

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
    return job, True


def create_fasta_input(
    content: str,
    *,
    filename: str = 'input.fasta',
) -> tuple[InputArtifact, bool]:
    if not isinstance(content, str):
        raise ValueError('FASTA content must be text')
    encoded = content.encode('utf-8')
    if not encoded.strip():
        raise ValueError('FASTA content must not be empty')
    if len(encoded) > settings.MAX_FASTA_INPUT_BYTES:
        raise ValueError(
            f'FASTA input exceeds maximum size of {settings.MAX_FASTA_INPUT_BYTES} bytes'
        )

    safe_filename = Path(filename).name.strip() or 'input.fasta'
    if len(safe_filename) > 255:
        raise ValueError('filename must be at most 255 characters')
    digest = hashlib.sha256(encoded).hexdigest()
    existing = InputArtifact.objects.filter(
        kind='fasta_input',
        sha256=digest,
    ).first()
    if existing is not None:
        artifact_root = Path(settings.ARTIFACT_ROOT).resolve()
        existing_path = (artifact_root / existing.storage_path).resolve()
        if not existing_path.is_relative_to(artifact_root):
            raise ValueError('stored FASTA input path is outside ARTIFACT_ROOT')
        if (
            not existing_path.is_file()
            or existing_path.stat().st_size != existing.size
            or hashlib.sha256(existing_path.read_bytes()).hexdigest() != existing.sha256
        ):
            existing_path.parent.mkdir(parents=True, exist_ok=True)
            repair_path = existing_path.parent / f'.{digest}.{uuid.uuid4().hex}.tmp'
            try:
                repair_path.write_bytes(encoded)
                os.replace(repair_path, existing_path)
            finally:
                repair_path.unlink(missing_ok=True)
        return existing, False

    artifact_root = Path(settings.ARTIFACT_ROOT).resolve()
    input_dir = artifact_root / 'inputs'
    input_dir.mkdir(parents=True, exist_ok=True)
    storage_path = Path('inputs') / f'{digest}.fasta'
    final_path = artifact_root / storage_path
    temp_path = input_dir / f'.{digest}.{uuid.uuid4().hex}.tmp'
    try:
        temp_path.write_bytes(encoded)
        os.replace(temp_path, final_path)
        try:
            with transaction.atomic():
                artifact = InputArtifact.objects.create(
                    kind='fasta_input',
                    filename=safe_filename,
                    storage_path=storage_path.as_posix(),
                    media_type='text/x-fasta',
                    size=len(encoded),
                    sha256=digest,
                )
        except IntegrityError:
            artifact = InputArtifact.objects.get(
                kind='fasta_input',
                sha256=digest,
            )
            return artifact, False
    finally:
        temp_path.unlink(missing_ok=True)
    return artifact, True


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
    from django.utils import timezone

    with transaction.atomic():
        locked_job = AnalysisJob.objects.select_for_update().get(id=job.id)
        if locked_job.status in TERMINAL_STATUSES:
            raise ValueError(f'job is already {locked_job.status}')

        previous_status = locked_job.status
        locked_job.status = AnalysisJob.Status.CANCELLED
        locked_job.stage = 'cancelled'
        locked_job.progress = None
        locked_job.lease_expires_at = None
        locked_job.finished_at = timezone.now()
        locked_job.save(
            update_fields=[
                'status',
                'stage',
                'progress',
                'lease_expires_at',
                'finished_at',
                'updated_at',
            ]
        )
        removed_from_queue = _delete_queued_task(locked_job.queue_task_id)
        add_event(
            locked_job,
            'job_cancelled',
            'Analysis job cancelled',
            {
                'previous_status': previous_status,
                'removed_from_queue': removed_from_queue,
            },
        )
    return locked_job


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


def input_artifact_payload(artifact: InputArtifact) -> dict[str, Any]:
    return {
        'input_artifact_id': str(artifact.id),
        'kind': artifact.kind,
        'filename': artifact.filename,
        'media_type': artifact.media_type,
        'size': artifact.size,
        'sha256': artifact.sha256,
        'metadata': artifact.metadata,
        'download_url': f'/api/inputs/{artifact.id}/download',
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

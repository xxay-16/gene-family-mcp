from uuid import UUID

from ninja import Router, Schema
from ninja.responses import Response

from jobs.models import AnalysisJob
from jobs.services import create_analysis_job, job_payload

router = Router(tags=['cis-elements'])


class SequenceIn(Schema):
    sequence: str


@router.post('/submit', response={202: dict})
def submit_sequence(request, payload: SequenceIn):
    try:
        job, created = create_analysis_job(
            AnalysisJob.AnalysisType.CIS_ELEMENTS,
            {'sequence': payload.sequence},
            idempotency_key=request.headers.get('Idempotency-Key', ''),
        )
    except ValueError as exc:
        return Response(
            {'error': {'code': 'INVALID_SEQUENCE', 'message': str(exc)}},
            status=422,
        )
    except RuntimeError as exc:
        return Response(
            {'error': {'code': 'QUEUE_UNAVAILABLE', 'message': str(exc)}},
            status=503,
        )
    return 202, {
        **job_payload(job),
        'task_id': str(job.id),
        'created': created,
    }


@router.get('/tasks/{task_id}')
def query_task(request, task_id: UUID):
    try:
        job = AnalysisJob.objects.prefetch_related('artifacts').get(id=task_id)
    except AnalysisJob.DoesNotExist:
        return Response(
            {
                'status': 'not_found',
                'task_id': str(task_id),
                'error': {'code': 'JOB_NOT_FOUND', 'message': 'Analysis job not found'},
            },
            status=404,
        )
    return {
        **job_payload(
            job,
            include_result=job.status == AnalysisJob.Status.SUCCEEDED,
        ),
        'task_id': str(job.id),
    }

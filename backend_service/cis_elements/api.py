from django_q.models import Failure, OrmQ, Success
from django_q.signing import SignedPackage
from django_q.tasks import async_task
from ninja import Router
from ninja.responses import Response
from pydantic import BaseModel

router = Router(tags=['cis-elements'])


class SequenceIn(BaseModel):
    sequence: str


def _find_queued_task(task_id: str):
    for queued_task in OrmQ.objects.all():
        try:
            payload = SignedPackage.loads(queued_task.payload)
        except Exception:
            continue
        if payload.get('id') == task_id:
            return queued_task
    return None


@router.post('/submit')
def submit_sequence(request, payload: SequenceIn):
    sequence = ''.join(payload.sequence.split()).upper()
    if not sequence:
        return Response({'error': 'sequence must not be empty'}, status=422)
    if any(base not in 'ACGTN' for base in sequence):
        return Response(
            {'error': 'sequence must contain only A, C, G, T or N'},
            status=422,
        )
    task_id = async_task('cis_elements.services.run_prediction_task', sequence)
    return {'task_id': task_id, 'status': 'queued'}


@router.get('/tasks/{task_id}')
def query_task(request, task_id: str):
    success_task = Success.objects.filter(id=task_id).first()
    if success_task:
        return {
            'status': 'success',
            'task_id': task_id,
            'result': success_task.result,
        }
    failure_task = Failure.objects.filter(id=task_id).first()
    if failure_task:
        return {
            'status': 'failed',
            'task_id': task_id,
            'error': str(failure_task.result),
        }
    if _find_queued_task(task_id) is not None:
        return {'status': 'queued', 'task_id': task_id}
    return Response(
        {'status': 'not_found', 'task_id': task_id, 'error': 'task not found'},
        status=404,
    )

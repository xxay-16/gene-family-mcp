from django_q.models import Failure, OrmQ, Success
from django_q.tasks import async_task
from ninja import Router
from pydantic import BaseModel

router = Router(tags=['cis-elements'])


class SequenceIn(BaseModel):
    sequence: str


@router.post('/submit')
def submit_sequence(request, payload: SequenceIn):
    task_id = async_task('cis_elements.services.run_prediction_task', payload.sequence)
    return {'task_id': task_id}


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
    if OrmQ.objects.filter(payload__contains=task_id).exists():
        return {'status': 'queued', 'task_id': task_id}
    return {'status': 'processing', 'task_id': task_id}

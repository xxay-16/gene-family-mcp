from django_q.tasks import async_task
from ninja import Router
from pydantic import BaseModel

router = Router(tags=['core'])


class QueueTaskIn(BaseModel):
    message: str


def sample_background_job(message: str) -> str:
    return f'processed: {message}'


@router.get('/health')
def health(request):
    return {'status': 'ok'}


@router.post('/tasks')
def enqueue_task(request, payload: QueueTaskIn):
    task_id = async_task(sample_background_job, payload.message)
    return {'task_id': task_id}

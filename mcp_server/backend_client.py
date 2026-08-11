import json
from typing import Any
from urllib import error, request

from .settings import BACKEND_API_URL, BACKEND_TIMEOUT, BACKEND_TOKEN


class BackendAPIError(RuntimeError):
    def __init__(self, status_code: int, detail: Any):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f'Backend API returned HTTP {status_code}: {detail}')


class BackendClient:
    def __init__(
        self,
        base_url: str = BACKEND_API_URL,
        timeout: float = BACKEND_TIMEOUT,
        token: str = BACKEND_TOKEN,
    ):
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        self.token = token

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        body = None
        headers = {'Accept': 'application/json'}
        if self.token:
            headers['Authorization'] = f'Bearer {self.token}'
        if extra_headers:
            headers.update(extra_headers)
        if payload is not None:
            body = json.dumps(payload).encode('utf-8')
            headers['Content-Type'] = 'application/json'

        api_request = request.Request(
            f'{self.base_url}{path}',
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with request.urlopen(api_request, timeout=self.timeout) as response:
                return json.loads(response.read().decode('utf-8'))
        except error.HTTPError as exc:
            response_body = exc.read().decode('utf-8', errors='replace')
            try:
                detail = json.loads(response_body)
            except json.JSONDecodeError:
                detail = response_body
            raise BackendAPIError(exc.code, detail) from exc
        except error.URLError as exc:
            raise RuntimeError(f'Cannot connect to backend API: {exc.reason}') from exc

    def health(self) -> dict[str, Any]:
        return self._request('GET', '/core/health')

    def capabilities(self) -> dict[str, Any]:
        return self._request('GET', '/core/capabilities')

    def create_job(
        self,
        analysis_type: str,
        parameters: dict[str, Any],
        idempotency_key: str = '',
    ) -> dict[str, Any]:
        headers = {'Idempotency-Key': idempotency_key} if idempotency_key else None
        return self._request(
            'POST',
            '/jobs',
            {
                'analysis_type': analysis_type,
                'parameters': parameters,
            },
            extra_headers=headers,
        )

    def create_fasta_input(
        self,
        content: str,
        filename: str = 'input.fasta',
    ) -> dict[str, Any]:
        return self._request(
            'POST',
            '/inputs/fasta',
            {'content': content, 'filename': filename},
        )

    def get_job(self, job_id: str) -> dict[str, Any]:
        return self._request('GET', f'/jobs/{job_id}')

    def get_job_result(self, job_id: str) -> dict[str, Any]:
        return self._request('GET', f'/jobs/{job_id}/result')

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        return self._request('POST', f'/jobs/{job_id}/cancel')

    def submit_cis_element_analysis(
        self,
        sequence: str,
        idempotency_key: str = '',
    ) -> dict[str, Any]:
        return self.create_job(
            'cis_elements',
            {'sequence': sequence},
            idempotency_key=idempotency_key,
        )

    def submit_fasta_validation(
        self,
        fasta: str,
        alphabet: str = 'auto',
        filename: str = 'input.fasta',
        idempotency_key: str = '',
    ) -> dict[str, Any]:
        input_artifact = self.create_fasta_input(fasta, filename)
        job = self.create_job(
            'fasta_validation',
            {
                'input_artifact_id': input_artifact['input_artifact_id'],
                'alphabet': alphabet,
            },
            idempotency_key=idempotency_key,
        )
        job['input_artifact'] = input_artifact
        return job

    def submit_multiple_sequence_alignment(
        self,
        artifact_id: str,
        strategy: str = 'auto',
        threads: int = 2,
        idempotency_key: str = '',
    ) -> dict[str, Any]:
        return self.create_job(
            'multiple_sequence_alignment',
            {
                'artifact_id': artifact_id,
                'strategy': strategy,
                'threads': threads,
            },
            idempotency_key=idempotency_key,
        )

    def submit_phylogenetic_tree(
        self,
        artifact_id: str,
        model: str = 'auto',
        threads: int = 2,
        idempotency_key: str = '',
    ) -> dict[str, Any]:
        return self.create_job(
            'phylogenetic_tree',
            {
                'artifact_id': artifact_id,
                'model': model,
                'threads': threads,
            },
            idempotency_key=idempotency_key,
        )

    def get_task_status(self, task_id: str) -> dict[str, Any]:
        return self.get_job(task_id)

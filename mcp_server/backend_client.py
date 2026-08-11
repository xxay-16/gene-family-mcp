import json
from typing import Any
from urllib import error, request

from .settings import BACKEND_API_URL, BACKEND_TIMEOUT


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
    ):
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = None
        headers = {'Accept': 'application/json'}
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
    ) -> dict[str, Any]:
        return self._request(
            'POST',
            '/jobs',
            {
                'analysis_type': analysis_type,
                'parameters': parameters,
            },
        )

    def get_job(self, job_id: str) -> dict[str, Any]:
        return self._request('GET', f'/jobs/{job_id}')

    def get_job_result(self, job_id: str) -> dict[str, Any]:
        return self._request('GET', f'/jobs/{job_id}/result')

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        return self._request('POST', f'/jobs/{job_id}/cancel')

    def submit_cis_element_analysis(self, sequence: str) -> dict[str, Any]:
        return self.create_job(
            'cis_elements',
            {'sequence': sequence},
        )

    def get_task_status(self, task_id: str) -> dict[str, Any]:
        return self.get_job(task_id)

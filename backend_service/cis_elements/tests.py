from unittest.mock import patch

from django.test import Client, TestCase


class CisElementAPITests(TestCase):
    def setUp(self):
        self.client = Client()

    @patch('cis_elements.api.async_task', return_value='task-123')
    def test_submit_normalizes_sequence(self, async_task_mock):
        response = self.client.post(
            '/api/cis-elements/submit',
            data={'sequence': 'acgt nn\n'},
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {'task_id': 'task-123', 'status': 'queued'},
        )
        async_task_mock.assert_called_once_with(
            'cis_elements.services.run_prediction_task',
            'ACGTNN',
        )

    def test_submit_rejects_invalid_sequence(self):
        response = self.client.post(
            '/api/cis-elements/submit',
            data={'sequence': 'ACGT-X'},
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 422)
        self.assertIn('error', response.json())

    def test_unknown_task_returns_404(self):
        response = self.client.get('/api/cis-elements/tasks/unknown-task')

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()['status'], 'not_found')

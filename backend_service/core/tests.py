from unittest.mock import patch

from django.test import Client, TestCase, override_settings


class HealthAPITests(TestCase):
    def test_health_endpoint(self):
        response = Client().get('/api/core/health')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {'status': 'ok', 'service': 'gene-family-backend'},
        )
        self.assertIn('X-Request-ID', response.headers)

    @override_settings(BACKEND_API_TOKEN='secret-token')
    def test_token_auth_protects_non_health_api(self):
        client = Client()

        unauthorized = client.get('/api/core/capabilities')
        authorized = client.get(
            '/api/core/capabilities',
            HTTP_AUTHORIZATION='Bearer secret-token',
        )
        health = client.get('/api/core/health')

        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(unauthorized.json()['error']['code'], 'UNAUTHORIZED')
        self.assertEqual(authorized.status_code, 200)
        alignment = authorized.json()['analysis_types'][
            'multiple_sequence_alignment'
        ]
        self.assertIn(alignment['status'], {'available', 'unavailable'})
        self.assertNotIn('resolved_executable', alignment['runtime'])
        self.assertNotIn('configured_executable', alignment['runtime'])
        tree = authorized.json()['analysis_types']['phylogenetic_tree']
        self.assertIn(tree['status'], {'available', 'unavailable'})
        self.assertNotIn('resolved_executable', tree['runtime'])
        self.assertEqual(health.status_code, 200)

    def test_ready_endpoint_checks_database_and_schedule(self):
        response = Client().get('/api/core/ready')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'ready')

    @patch('core.api.Schedule.objects.filter')
    def test_ready_endpoint_reports_missing_schedule(self, filter_mock):
        filter_mock.return_value.exclude.return_value.exists.return_value = False

        response = Client().get('/api/core/ready')

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['status'], 'not_ready')

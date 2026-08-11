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
        self.assertEqual(health.status_code, 200)

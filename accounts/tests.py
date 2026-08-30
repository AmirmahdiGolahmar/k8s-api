from django.contrib.auth import get_user_model
from django.test import Client, TestCase


class AuthEndpointTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('alice', password='pw123456', is_staff=True)

    def test_login_requires_valid_credentials(self):
        response = self.client.post('/auth/login/', {'username': 'alice', 'password': 'wrong'})
        self.assertEqual(response.status_code, 401)

    def test_login_logout_and_me_flow(self):
        response = self.client.get('/auth/me/')
        self.assertEqual(response.status_code, 403)

        response = self.client.post('/auth/login/', {'username': 'alice', 'password': 'pw123456'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'username': 'alice', 'is_staff': True})

        response = self.client.get('/auth/me/')
        self.assertEqual(response.status_code, 200)

        response = self.client.post('/auth/logout/')
        self.assertEqual(response.status_code, 204)

        response = self.client.get('/auth/me/')
        self.assertEqual(response.status_code, 403)

    def test_authenticated_requests_enforce_csrf(self):
        # SessionAuthentication only enforces CSRF once a session already
        # exists -- it protects an already-logged-in user from cross-site
        # abuse, not the login request itself (nothing to protect yet at
        # that point, which is why login succeeds without a token above).
        # logout requires an existing session, so it's the right endpoint
        # to prove CSRF is actually enforced for real, authenticated calls.
        client = Client(enforce_csrf_checks=True)
        login_response = client.post('/auth/login/', {'username': 'alice', 'password': 'pw123456'})
        self.assertEqual(login_response.status_code, 200)

        response = client.post('/auth/logout/')
        self.assertEqual(response.status_code, 403, 'logout without a CSRF token should be rejected')

        csrf_response = client.get('/auth/csrf/')
        token = csrf_response.json()['csrfToken']
        response = client.post('/auth/logout/', HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 204)

    def test_cluster_endpoint_requires_authentication(self):
        # Regression test for the gap this app closes: every CRUD endpoint
        # (cluster/app/namespace/backup) had no permission_classes and no
        # DEFAULT_PERMISSION_CLASSES, so it was open to anyone, unauthenticated.
        response = self.client.get('/cluster/')
        self.assertEqual(response.status_code, 403)

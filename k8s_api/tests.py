import os
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.test import TestCase, override_settings


class DeploymentReadinessTests(TestCase):
    """Sanity checks for the production-prep changes made ahead of the k8s
    deployment: env-driven settings, whitenoise, gunicorn, collectstatic,
    DEBUG=False, and the /healthz/ probe endpoint. Running this suite also
    runs Django's own system checks (manage.py test always checks() first),
    so a broken setting fails here before it ever reaches a container.
    """

    def test_data_dir_follows_env(self):
        expected = Path(os.environ.get('DATA_DIR', settings.BASE_DIR))
        self.assertEqual(settings.DATA_DIR, expected)
        self.assertEqual(settings.BACKUP_DIR, expected / 'backup_dumps')

    def test_allowed_hosts_and_csrf_origins_are_lists(self):
        self.assertIsInstance(settings.ALLOWED_HOSTS, list)
        self.assertIsInstance(settings.CSRF_TRUSTED_ORIGINS, list)

    def test_whitenoise_is_wired_in(self):
        self.assertIn('whitenoise.middleware.WhiteNoiseMiddleware', settings.MIDDLEWARE)
        self.assertIn('whitenoise', settings.STORAGES['staticfiles']['BACKEND'])

    def test_gunicorn_and_whitenoise_are_installed(self):
        import gunicorn  # noqa: F401
        import whitenoise  # noqa: F401

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Actually populates STATIC_ROOT (not --dry-run) -- the exact command
        # the Dockerfile runs at build time. Needed for real, not just as a
        # check: ManifestStaticFilesStorage below raises on any {% static %}
        # tag if this was skipped, which is what test_key_endpoints_survive_debug_false
        # is there to catch.
        call_command('collectstatic', interactive=False, verbosity=0)

    def test_collectstatic_runs_cleanly(self):
        manifest = settings.STATIC_ROOT / 'staticfiles.json'
        self.assertTrue(manifest.exists(), f'{manifest} missing -- collectstatic did not run in setUpClass')

    def test_healthz_endpoint(self):
        response = self.client.get('/healthz/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'status': 'ok'})

    @override_settings(DEBUG=False, ALLOWED_HOSTS=['testserver'])
    def test_key_endpoints_survive_debug_false(self):
        for path in ['/healthz/', '/docs/', '/schema/', '/admin/login/']:
            response = self.client.get(path)
            self.assertLess(
                response.status_code, 500,
                f'{path} returned {response.status_code} with DEBUG=False',
            )

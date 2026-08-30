from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from .models import App, Cluster, Namespace


class ClusterPermissionTests(TestCase):
    """Regular users may read Clusters (needed to pick one for
    Namespace/App CRUD) but only staff may add/edit/delete one -- that's
    the one place a kubeconfig gets submitted."""

    def setUp(self):
        self.user = get_user_model().objects.create_user('bob', password='pw123456', is_staff=False)
        self.admin = get_user_model().objects.create_user('admin', password='pw123456', is_staff=True)
        self.cluster = Cluster.objects.create(name='prod')

    def test_regular_user_can_list_and_retrieve(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get('/cluster/').status_code, 200)
        self.assertEqual(self.client.get(f'/cluster/{self.cluster.pk}/').status_code, 200)

    def test_regular_user_cannot_write(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.post('/cluster/', {'name': 'new'}).status_code, 403)
        self.assertEqual(
            self.client.delete(f'/cluster/{self.cluster.pk}/').status_code, 403,
        )

    def test_admin_can_write(self):
        self.client.force_login(self.admin)
        response = self.client.post('/cluster/', {'name': 'new-cluster'})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.client.delete(f'/cluster/{self.cluster.pk}/').status_code, 204)

    def test_anonymous_user_is_rejected(self):
        self.assertEqual(self.client.get('/cluster/').status_code, 403)

    def test_regular_user_can_reach_namespace_and_app_endpoints(self):
        # Only checks the permission layer lets a non-staff user through --
        # not a full create flow, which would need a real/mocked k8s
        # cluster. A 400 here (missing query param) still proves it's past
        # the 403 permission check; that's what's under test.
        self.client.force_login(self.user)
        self.assertNotEqual(self.client.get('/namespace/').status_code, 403)
        self.assertNotEqual(self.client.get('/app/').status_code, 403)


class ClusterAccessibilityTests(TestCase):
    """is_accessible=False hides a Cluster from regular users entirely,
    except whoever is explicitly listed in allowed_users. Staff always see
    everything regardless."""

    def setUp(self):
        self.userA = get_user_model().objects.create_user('userA', password='pw123456')
        self.userB = get_user_model().objects.create_user('userB', password='pw123456')
        self.admin = get_user_model().objects.create_user('clusteradmin', password='pw123456', is_staff=True)
        self.restricted = Cluster.objects.create(name='restricted-cluster', is_accessible=False)
        self.restricted.allowed_users.add(self.userA)
        self.public = Cluster.objects.create(name='public-cluster')  # is_accessible defaults True

    def _cluster_names(self, user):
        self.client.force_login(user)
        return {c['name'] for c in self.client.get('/cluster/').json()}

    def test_default_accessible_cluster_visible_to_everyone(self):
        self.assertIn('public-cluster', self._cluster_names(self.userA))
        self.assertIn('public-cluster', self._cluster_names(self.userB))

    def test_restricted_cluster_visible_only_to_allowed_user(self):
        self.assertIn('restricted-cluster', self._cluster_names(self.userA))
        self.assertNotIn('restricted-cluster', self._cluster_names(self.userB))

    def test_staff_sees_restricted_cluster_regardless(self):
        self.assertIn('restricted-cluster', self._cluster_names(self.admin))

    def test_restricted_cluster_404s_on_direct_retrieve_for_disallowed_user(self):
        self.client.force_login(self.userB)
        response = self.client.get(f'/cluster/{self.restricted.pk}/')
        self.assertEqual(response.status_code, 404)

    def test_only_staff_can_change_accessibility(self):
        self.client.force_login(self.userA)
        response = self.client.patch(
            f'/cluster/{self.public.pk}/',
            data={'is_accessible': False},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 403)

    def test_staff_can_toggle_accessibility_and_allowed_users(self):
        self.client.force_login(self.admin)
        response = self.client.patch(
            f'/cluster/{self.public.pk}/',
            data={'is_accessible': False, 'allowed_users': [self.userB.pk]},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)

        self.assertNotIn('public-cluster', self._cluster_names(self.userA))
        self.assertIn('public-cluster', self._cluster_names(self.userB))


class NamespaceAppOwnershipTests(TestCase):
    """User A's Namespaces/Apps are invisible and undeletable to user B;
    staff can see and manage everyone's."""

    def setUp(self):
        self.user_a = get_user_model().objects.create_user('alice2', password='pw123456')
        self.user_b = get_user_model().objects.create_user('bob2', password='pw123456')
        self.admin = get_user_model().objects.create_user('admin2', password='pw123456', is_staff=True)
        self.cluster = Cluster.objects.create(name='shared-cluster')
        self.namespace = Namespace.objects.create(cluster=self.cluster, name='ns-a', owner=self.user_a)
        self.app = App.objects.create(cluster=self.cluster, namespace='ns-a', name='app-a', owner=self.user_a)

    def test_owner_sees_own_namespace_and_app(self):
        self.client.force_login(self.user_a)
        names = [row['name'] for row in self.client.get(f'/namespace/?cluster_id={self.cluster.pk}').json()]
        self.assertIn('ns-a', names)
        names = [row['name'] for row in self.client.get(f'/app/?cluster_id={self.cluster.pk}').json()]
        self.assertIn('app-a', names)

    def test_other_user_cannot_see_namespace_or_app(self):
        self.client.force_login(self.user_b)
        names = [row['name'] for row in self.client.get(f'/namespace/?cluster_id={self.cluster.pk}').json()]
        self.assertNotIn('ns-a', names)
        names = [row['name'] for row in self.client.get(f'/app/?cluster_id={self.cluster.pk}').json()]
        self.assertNotIn('app-a', names)

    def test_other_user_cannot_delete_namespace_or_app(self):
        self.client.force_login(self.user_b)
        response = self.client.delete(f'/namespace/{self.namespace.pk}/')
        self.assertEqual(response.status_code, 403)
        self.namespace.refresh_from_db()
        self.assertEqual(self.namespace.status, Namespace.Status.ACTIVE)

        response = self.client.delete(f'/app/{self.app.pk}/')
        self.assertEqual(response.status_code, 403)
        self.app.refresh_from_db()
        self.assertEqual(self.app.status, App.Status.ACTIVE)

    def test_admin_sees_everyones_namespace_and_app(self):
        self.client.force_login(self.admin)
        names = [row['name'] for row in self.client.get(f'/namespace/?cluster_id={self.cluster.pk}').json()]
        self.assertIn('ns-a', names)
        names = [row['name'] for row in self.client.get(f'/app/?cluster_id={self.cluster.pk}').json()]
        self.assertIn('app-a', names)


class LiveNamespaceAccessTests(TestCase):
    """NamespaceDetailView (live k8s read/patch by name) has no DB row to
    check ownership against by default -- this is the gap where a regular
    user who knew a namespace's name could read/patch any namespace's live
    k8s state, bypassing the ownership check on the list/delete views."""

    def setUp(self):
        self.owner = get_user_model().objects.create_user('carol', password='pw123456')
        self.other = get_user_model().objects.create_user('dave', password='pw123456')
        self.admin = get_user_model().objects.create_user('admin3', password='pw123456', is_staff=True)
        self.cluster = Cluster.objects.create(name='live-cluster')
        Namespace.objects.create(cluster=self.cluster, name='tracked-ns', owner=self.owner)

    def test_non_owner_blocked_from_get(self):
        self.client.force_login(self.other)
        response = self.client.get(f'/namespace/tracked-ns/?cluster={self.cluster.pk}')
        self.assertEqual(response.status_code, 403)

    def test_non_owner_blocked_from_patch(self):
        self.client.force_login(self.other)
        response = self.client.patch(
            f'/namespace/tracked-ns/?cluster={self.cluster.pk}',
            data={'labels': {'x': 'y'}},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 403)

    def test_untracked_namespace_blocked_for_regular_user(self):
        # Never went through NamespaceListCreateView.post -- no DB row at
        # all, so nobody but staff may touch it.
        self.client.force_login(self.other)
        response = self.client.get(f'/namespace/never-tracked/?cluster={self.cluster.pk}')
        self.assertEqual(response.status_code, 403)

    def test_may_access_live_namespace_helper(self):
        # Exercises the owner/staff/no-cluster branches directly rather than
        # through the view, since a real allowed request would go on to
        # call the (unmocked) kubernetes client.
        from .views import may_access_live_namespace

        self.assertTrue(may_access_live_namespace(self.cluster, 'tracked-ns', self.owner))
        self.assertFalse(may_access_live_namespace(self.cluster, 'tracked-ns', self.other))
        self.assertTrue(may_access_live_namespace(self.cluster, 'tracked-ns', self.admin))
        self.assertTrue(may_access_live_namespace(self.cluster, 'anything-untracked', self.admin))
        self.assertFalse(may_access_live_namespace(None, 'tracked-ns', self.other))


class NamespaceAccessManagementTests(TestCase):
    """PATCH /namespace/<pk>/ (NamespaceDeleteView.patch) -- admin-only
    control over is_accessible/allowed_users, independent of ownership."""

    def setUp(self):
        self.owner = get_user_model().objects.create_user('grace', password='pw123456')
        self.stranger = get_user_model().objects.create_user('heidi', password='pw123456')
        self.admin = get_user_model().objects.create_user('nsadmin', password='pw123456', is_staff=True)
        self.cluster = Cluster.objects.create(name='ns-access-cluster')
        self.namespace = Namespace.objects.create(cluster=self.cluster, name='owned-ns', owner=self.owner)

    def test_non_staff_cannot_patch_even_the_owner(self):
        self.client.force_login(self.owner)
        response = self.client.patch(
            f'/namespace/{self.namespace.pk}/',
            data={'is_accessible': False},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 403)

    def test_admin_locking_accessible_blocks_the_owner(self):
        self.client.force_login(self.admin)
        response = self.client.patch(
            f'/namespace/{self.namespace.pk}/',
            data={'is_accessible': False},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)

        self.client.force_login(self.owner)
        names = [n['name'] for n in self.client.get(f'/namespace/?cluster_id={self.cluster.pk}').json()]
        self.assertNotIn('owned-ns', names)

    def test_allowed_users_grants_access_to_a_non_owner(self):
        self.client.force_login(self.admin)
        response = self.client.patch(
            f'/namespace/{self.namespace.pk}/',
            data={'is_accessible': False, 'allowed_user_ids': [self.stranger.pk]},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)

        # Owner is locked out (is_accessible=False overrides ownership)...
        self.client.force_login(self.owner)
        names = [n['name'] for n in self.client.get(f'/namespace/?cluster_id={self.cluster.pk}').json()]
        self.assertNotIn('owned-ns', names)

        # ...but the explicitly allow-listed non-owner can still see it.
        self.client.force_login(self.stranger)
        names = [n['name'] for n in self.client.get(f'/namespace/?cluster_id={self.cluster.pk}').json()]
        self.assertIn('owned-ns', names)


class AppRefreshStatusTests(TestCase):
    """POST /app/<pk>/refresh/ -- on-demand version of
    clusters.tasks.sync_app_status for a single App."""

    def setUp(self):
        self.owner = get_user_model().objects.create_user('erin', password='pw123456')
        self.other = get_user_model().objects.create_user('frank', password='pw123456')
        self.cluster = Cluster.objects.create(name='refresh-cluster')
        self.app = App.objects.create(
            cluster=self.cluster, namespace='ns', name='app-x', owner=self.owner, status=App.Status.ACTIVE,
        )

    def test_non_owner_blocked(self):
        self.client.force_login(self.other)
        response = self.client.post(f'/app/{self.app.pk}/refresh/')
        self.assertEqual(response.status_code, 403)

    def test_missing_app_404(self):
        self.client.force_login(self.owner)
        response = self.client.post('/app/999999/refresh/')
        self.assertEqual(response.status_code, 404)

    @patch('clusters.views.deployment_present')
    @patch('clusters.views.get_apps_v1_client')
    def test_flips_active_to_missing_when_deployment_gone(self, mock_client, mock_present):
        mock_present.return_value = False  # Deployment genuinely absent from k8s
        self.client.force_login(self.owner)

        response = self.client.post(f'/app/{self.app.pk}/refresh/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'missing')
        self.app.refresh_from_db()
        self.assertEqual(self.app.status, App.Status.MISSING)

    @patch('clusters.views.deployment_present')
    @patch('clusters.views.get_apps_v1_client')
    def test_stays_active_when_deployment_present(self, mock_client, mock_present):
        mock_present.return_value = True
        self.client.force_login(self.owner)

        response = self.client.post(f'/app/{self.app.pk}/refresh/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'active')

    @patch('clusters.views.deployment_present')
    @patch('clusters.views.get_apps_v1_client')
    def test_inconclusive_check_leaves_status_untouched(self, mock_client, mock_present):
        mock_present.return_value = None  # network hiccup, no definitive answer
        self.client.force_login(self.owner)

        response = self.client.post(f'/app/{self.app.pk}/refresh/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'active')

    def test_deleting_app_is_never_checked(self):
        self.app.status = App.Status.DELETING
        self.app.save(update_fields=['status'])
        self.client.force_login(self.owner)

        # No mocking at all here -- if the view tried to reach a real k8s
        # client for a DELETING app, this would fail/hang instead of
        # returning cleanly, since DELETING must never be touched.
        response = self.client.post(f'/app/{self.app.pk}/refresh/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'deleting')

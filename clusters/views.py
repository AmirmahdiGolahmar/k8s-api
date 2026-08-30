import json
import time

from urllib3 import request
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema, inline_serializer
from kubernetes import client as k8s_client
from kubernetes.client.exceptions import ApiException
from rest_framework import permissions, serializers, status, viewsets
from rest_framework.generics import get_object_or_404
from rest_framework.response import Response
from rest_framework.views import APIView

from .k8s_client import get_apps_v1_client, get_core_v1_client
from .models import App, Cluster, Namespace
from .permissions import IsAdminOrReadOnly
from .serializers import AppRecordSerializer, ClusterSerializer, NamespaceRecordSerializer, NamespaceSerializer

# Bounds how long we wait for a k8s API *response*, so a stalled link
# surfaces as an error instead of hanging the request forever.
#
# Writes get a much shorter bound than reads on purpose: a write's response
# body is not needed for correctness (deployment_exists() below confirms the
# outcome for a few hundred bytes), and the request itself reaches the API
# server long before the reply comes back. On a link that cannot carry a
# multi-KB reply, waiting a long timeout for a response that will never
# arrive just adds dead latency to every single create.
K8S_WRITE_TIMEOUT = 5
K8S_READ_TIMEOUT = 10

# deployment_exists() retry budget. The check is small and fast, so a few
# tries cost little; the delay also gives a still-in-flight write time to
# land before we conclude it never happened.
VERIFY_ATTEMPTS = 3
VERIFY_BACKOFF_SECONDS = 1


class ClusterViewSet(viewsets.ModelViewSet):
    # Present alongside get_queryset() purely for schema/router
    # introspection (e.g. path parameter typing) -- get_queryset() below is
    # what actually runs and is filtered per-request, this is never used at
    # request time since get_queryset() always takes precedence.
    queryset = Cluster.objects.all()
    serializer_class = ClusterSerializer
    # Any authenticated user can list/retrieve (to pick a cluster for
    # namespace/app CRUD); only staff can add/edit/delete one.
    permission_classes = [permissions.IsAuthenticated, IsAdminOrReadOnly]

    def get_queryset(self):
        queryset = Cluster.objects.all()
        if self.request.user.is_staff:
            return queryset
        # is_accessible=False hides a cluster from everyone except staff
        # and whoever's explicitly listed in allowed_users.
        return queryset.filter(Q(is_accessible=True) | Q(allowed_users=self.request.user)).distinct()

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)


def resolve_cluster(request):
    """Pick the Cluster a namespace request should target.

    ?cluster=<id> wins if given, otherwise the cluster flagged is_default,
    otherwise None (get_core_v1_client falls back to the local kubeconfig).
    """
    cluster_id = request.query_params.get('cluster')
    if cluster_id:
        return get_object_or_404(Cluster, pk=cluster_id)
    return Cluster.objects.filter(is_default=True).first()


def user_can_access_namespace(namespace, user):
    """The single access rule for a tracked Namespace row, used by every
    view that reads/manages one (list, delete, live detail):

      staff, OR (owner AND is_accessible), OR explicitly in allowed_users.

    is_accessible is an admin override independent of ownership: it can
    lock the *owner* out of their own namespace without deleting it.
    allowed_users is a separate exception list -- being in it grants
    access regardless of ownership or is_accessible, which is also how a
    non-owner can be granted access to someone else's namespace.
    """
    if user.is_staff:
        return True
    if namespace.owner_id == user.id and namespace.is_accessible:
        return True
    return namespace.allowed_users.filter(pk=user.id).exists()


def may_access_live_namespace(cluster, name, user):
    """Gate for NamespaceDetailView (live k8s read/patch by name), which
    otherwise has no DB row to check ownership against -- without this, a
    regular user who knows a namespace's name could read/patch any
    namespace's live k8s state, bypassing the same-named check that
    protects the list/delete views (which go through the tracked DB row).

    Staff can always proceed. Everyone else needs a tracked DB row for this
    exact (cluster, name) that user_can_access_namespace approves -- an
    untracked namespace (created outside this backend, or a legacy
    pre-ownership row with no owner) is nobody's, so it's staff-only.
    """
    if user.is_staff:
        return True
    if cluster is None:
        return False
    try:
        namespace = Namespace.objects.get(cluster=cluster, name=name)
    except Namespace.DoesNotExist:
        return False
    return user_can_access_namespace(namespace, user)


def namespace_to_dict(ns):
    return {
        'name': ns.metadata.name,
        'uid': ns.metadata.uid,
        'status': ns.status.phase if ns.status else None,
        'labels': ns.metadata.labels or {},
        'annotations': ns.metadata.annotations or {},
        'created_at': ns.metadata.creation_timestamp,
    }


def api_exception_response(exc):
    detail = exc.reason or str(exc)
    payload = None
    if exc.body:
        try:
            payload = json.loads(exc.body)
        except (TypeError, ValueError):
            payload = None

    data = {'detail': detail}
    if isinstance(payload, dict):
        data['detail'] = payload.get('message', detail)
        if payload.get('reason'):
            data['reason'] = payload['reason']
        if payload.get('code'):
            data['code'] = payload['code']

    return Response(data, status=exc.status or status.HTTP_400_BAD_REQUEST)


class NamespaceListCreateView(APIView):
    """List/create backend-tracked namespaces. Source of truth = Database:
    GET only ever returns namespaces this backend created and recorded, and
    POST only records a namespace once Kubernetes has actually created it —
    a failed k8s create never leaves a row in the DB (doc sections 3.1, 3.4)."""

    @extend_schema(
        parameters=[OpenApiParameter('cluster_id', int, required=True)],
        responses=NamespaceRecordSerializer(many=True),
    )
    def get(self, request):
        cluster_id = request.query_params.get('cluster_id')
        if not cluster_id:
            return Response(
                {'detail': 'cluster_id query parameter is required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        namespaces = Namespace.objects.filter(cluster_id=cluster_id)
        # See user_can_access_namespace: owns-it-and-accessible, OR
        # explicitly allow-listed. Staff see everything.
        if not request.user.is_staff:
            namespaces = namespaces.filter(
                Q(owner=request.user, is_accessible=True) | Q(allowed_users=request.user)
            ).distinct()
        return Response(NamespaceRecordSerializer(namespaces, many=True).data)

    @extend_schema(
        request=inline_serializer('NamespaceCreateRequest', fields={
            'cluster_id': serializers.IntegerField(),
            'name': serializers.CharField(),
        }),
        responses={201: NamespaceRecordSerializer},
    )
    def post(self, request):
        cluster_id = request.data.get('cluster_id')
        if not cluster_id:
            return Response(
                {'detail': 'cluster_id is required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        cluster = get_object_or_404(Cluster, pk=cluster_id)

        serializer = NamespaceRecordSerializer(data={'name': request.data.get('name')})
        serializer.is_valid(raise_exception=True)
        name = serializer.validated_data['name']

        try:
            v1 = get_core_v1_client(cluster)
            created = v1.create_namespace(
                k8s_client.V1Namespace(metadata=k8s_client.V1ObjectMeta(name=name))
            )
        except ApiException as exc:
            return api_exception_response(exc)
        except Exception:
            # Anything short of an ApiException here (DNS/connect/TLS/auth
            # config failures) means we couldn't reach the cluster at all —
            # distinct from k8s reachable-but-rejecting-the-request.
            return Response(
                {'detail': 'Unable to reach the Kubernetes cluster.'},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        # Only persist once k8s creation actually succeeded, so a rejected
        # or failed create never leaves an orphan row in the DB.
        namespace = Namespace.objects.create(
            cluster=cluster, name=name, uid=created.metadata.uid, owner=request.user,
        )
        return Response(NamespaceRecordSerializer(namespace).data, status=status.HTTP_201_CREATED)


class NamespaceDetailView(APIView):
    """Live read/patch by k8s name — unchanged by the Namespace-model rework.
    Deletion moved to NamespaceDeleteView (DB id, doc 3.5) since it now needs
    to coordinate with the tracked DB row rather than just proxying k8s."""

    @extend_schema(
        parameters=[OpenApiParameter('cluster', int, description='Cluster id; defaults to the cluster flagged is_default.')],
        responses={200: NamespaceSerializer, 403: None},
    )
    def get(self, request, name):
        cluster = resolve_cluster(request)
        if not may_access_live_namespace(cluster, name, request.user):
            return Response(status=status.HTTP_403_FORBIDDEN)
        v1 = get_core_v1_client(cluster)
        try:
            ns = v1.read_namespace(name)
        except ApiException as exc:
            return api_exception_response(exc)
        return Response(NamespaceSerializer(namespace_to_dict(ns)).data)

    @extend_schema(
        parameters=[OpenApiParameter('cluster', int, description='Cluster id; defaults to the cluster flagged is_default.')],
        request=NamespaceSerializer,
        responses={200: NamespaceSerializer, 403: None},
    )
    def patch(self, request, name):
        cluster = resolve_cluster(request)
        if not may_access_live_namespace(cluster, name, request.user):
            return Response(status=status.HTTP_403_FORBIDDEN)

        serializer = NamespaceSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)

        v1 = get_core_v1_client(cluster)
        body = {'metadata': {}}
        if 'labels' in serializer.validated_data:
            body['metadata']['labels'] = serializer.validated_data['labels']
        if 'annotations' in serializer.validated_data:
            body['metadata']['annotations'] = serializer.validated_data['annotations']
        try:
            ns = v1.patch_namespace(name, body)
        except ApiException as exc:
            return api_exception_response(exc)
        return Response(NamespaceSerializer(namespace_to_dict(ns)).data)

    put = patch


class NamespaceLiveListView(APIView):
    """Staff-only: every namespace that actually exists in the cluster
    right now, read straight from Kubernetes -- not just the ones tracked
    in the DB (NamespaceListCreateView.get only ever shows namespaces
    created *through this app*). This is the only way to see cluster-system
    namespaces (kube-system, default, kube-public, kube-node-lease, ...)
    or anything created outside the app (kubectl, another tool)."""

    @extend_schema(
        parameters=[OpenApiParameter('cluster_id', int, required=True)],
        responses={200: NamespaceSerializer(many=True), 403: None},
    )
    def get(self, request):
        if not request.user.is_staff:
            return Response(status=status.HTTP_403_FORBIDDEN)

        cluster_id = request.query_params.get('cluster_id')
        if not cluster_id:
            return Response(
                {'detail': 'cluster_id query parameter is required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        cluster = get_object_or_404(Cluster, pk=cluster_id)

        try:
            v1 = get_core_v1_client(cluster)
            live = v1.list_namespace(_request_timeout=K8S_READ_TIMEOUT)
        except ApiException as exc:
            return api_exception_response(exc)
        except Exception:
            return Response(
                {'detail': 'Unable to reach the Kubernetes cluster.'},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response(NamespaceSerializer([namespace_to_dict(ns) for ns in live.items], many=True).data)


def app_labels(app_name):
    return {'app': app_name}


def deployment_present(apps_v1, name, namespace):
    """One existence check: True (present), False (404, genuinely absent), or
    None (no clean answer).

    Deliberately reads the *scale* subresource rather than the Deployment
    itself: a full V1Deployment response is several KB, and on links that
    can't carry a multi-KB response back (see K8S_WRITE_TIMEOUT) that read
    times out exactly like the write it is trying to verify, making the check
    useless. V1Scale is a few hundred bytes, so it comes back reliably and
    still gives a definitive answer.
    """
    try:
        apps_v1.read_namespaced_deployment_scale(
            name, namespace, _request_timeout=K8S_READ_TIMEOUT,
        )
        return True
    except ApiException as exc:
        return False if exc.status == 404 else None
    except Exception:
        return None


def deployment_appeared(apps_v1, name, namespace):
    """Confirm a create whose response we never received. Polls while the
    Deployment looks absent, since the write may still have been in flight.
    """
    for attempt in range(VERIFY_ATTEMPTS):
        if attempt:
            time.sleep(VERIFY_BACKOFF_SECONDS)
        if deployment_present(apps_v1, name, namespace) is True:
            return True
    return False


def deployment_gone(apps_v1, name, namespace):
    """Confirm a delete whose response we never received. Polls while the
    Deployment is still visible, because deletion is not instantaneous -- a
    single check right after the call can still see the object and would
    wrongly report the delete as failed.
    """
    for attempt in range(VERIFY_ATTEMPTS):
        if attempt:
            time.sleep(VERIFY_BACKOFF_SECONDS)
        if deployment_present(apps_v1, name, namespace) is False:
            return True
    return False


def build_deployment(app, labels):
    return k8s_client.V1Deployment(
        metadata=k8s_client.V1ObjectMeta(name=app.name),
        spec=k8s_client.V1DeploymentSpec(
            replicas=app.replicas,
            selector=k8s_client.V1LabelSelector(match_labels=labels),
            template=k8s_client.V1PodTemplateSpec(
                metadata=k8s_client.V1ObjectMeta(labels=labels),
                spec=k8s_client.V1PodSpec(
                    containers=[
                        k8s_client.V1Container(
                            name=app.name,
                            image=app.image,
                            ports=[k8s_client.V1ContainerPort(container_port=80)],
                        )
                    ]
                ),
            ),
        ),
    )


class AppListCreateView(APIView):
    """List/create backend-tracked apps (a Deployment). Source of truth =
    Database, same pattern as NamespaceListCreateView: GET only returns apps
    this backend created and recorded, and POST only records an app once the
    Deployment has actually been created in Kubernetes.
    """

    @extend_schema(
        parameters=[
            OpenApiParameter('cluster_id', int, required=True),
            OpenApiParameter('namespace', str),
        ],
        responses=AppRecordSerializer(many=True),
    )
    def get(self, request):
        cluster_id = request.query_params.get('cluster_id')
        if not cluster_id:
            return Response(
                {'detail': 'cluster_id query parameter is required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        apps = App.objects.filter(cluster_id=cluster_id)
        namespace = request.query_params.get('namespace')
        if namespace:
            apps = apps.filter(namespace=namespace)
        # Same per-owner visibility rule as NamespaceListCreateView.get.
        if not request.user.is_staff:
            apps = apps.filter(owner=request.user)
        return Response(AppRecordSerializer(apps, many=True).data)

    @extend_schema(
        request=inline_serializer('AppCreateRequest', fields={
            'cluster_id': serializers.IntegerField(),
            'name': serializers.CharField(),
            'namespace': serializers.CharField(),
            'image': serializers.CharField(),
            'replicas': serializers.IntegerField(required=False),
        }),
        responses={201: AppRecordSerializer, 409: OpenApiResponse(description='An app with this name already exists in the namespace.')},
    )
    def post(self, request):
        cluster_id = request.data.get('cluster_id')
        if not cluster_id:
            return Response(
                {'detail': 'cluster_id is required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        cluster = get_object_or_404(Cluster, pk=cluster_id)

        serializer = AppRecordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # Build via the model so unspecified image/replicas pick up their
        # field defaults -- DRF only marks fields with a model default as
        # required=False, it doesn't inject the default into validated_data.
        app = App(cluster=cluster, owner=request.user, **serializer.validated_data)
        name, namespace = app.name, app.namespace

        print(f"name = {name}")

        # Reject a duplicate before touching Kubernetes. AppRecordSerializer
        # can't enforce the model's unique_together for us because it doesn't
        # expose `cluster`, so without this an existing row would only surface
        # as an IntegrityError *after* the Deployment had been created.
        if App.objects.filter(cluster=cluster, namespace=namespace, name=name).exists():
            return Response(
                {'detail': f'An app named "{name}" already exists in namespace "{namespace}" on this cluster.'},
                status=status.HTTP_409_CONFLICT,
            )

        labels = app_labels(name)

        apps_v1 = get_apps_v1_client(cluster)

        print(f"apps_v1 = {apps_v1}")

        try:
            apps_v1.create_namespaced_deployment(
                namespace, build_deployment(app, labels), _request_timeout=K8S_WRITE_TIMEOUT,
            )
        except ApiException as exc:
            print(exc)
            return api_exception_response(exc)
        except Exception:
            # The request may have actually reached k8s even though we never
            # got a response back (seen in practice on this cluster) -- check
            # before reporting a failure that would otherwise leave an
            # untracked Deployment behind.
            if not deployment_appeared(apps_v1, name, namespace):
                return Response(
                    {'detail': 'Unable to reach the Kubernetes cluster.'},
                    status=status.HTTP_502_BAD_GATEWAY,
                )

        try:
            app.save()
        except IntegrityError:
            # Lost a race with a concurrent create of the same name. The
            # Deployment now in the cluster is the winner's, so leave it be
            # rather than deleting another request's resource.
            return Response(
                {'detail': f'An app named "{name}" already exists in namespace "{namespace}" on this cluster.'},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(AppRecordSerializer(app).data, status=status.HTTP_201_CREATED)


class AppDeleteView(APIView):
    """Delete a backend-tracked app by its DB id. Mirrors
    NamespaceDeleteView's claim-then-delete concurrency pattern: the row is
    claimed (ACTIVE -> DELETING) inside a short locked transaction before
    touching Kubernetes, then the Deployment is deleted outside the lock,
    tolerating an already-gone (404) resource.

    Same known gap as NamespaceDeleteView: a crash after claiming the row but
    before the k8s delete/final DB delete finish leaves the row stuck in
    DELETING forever. Not reconciled here, out of scope for this exercise.
    """

    @extend_schema(
        responses={
            204: None,
            404: None,
            409: OpenApiResponse(description='App deletion is already in progress.'),
        },
    )
    def delete(self, request, pk):
        with transaction.atomic():
            try:
                app = App.objects.select_for_update().get(pk=pk)
            except App.DoesNotExist:
                return Response(status=status.HTTP_404_NOT_FOUND)

            if app.owner_id != request.user.id and not request.user.is_staff:
                return Response(status=status.HTTP_403_FORBIDDEN)

            if app.status == App.Status.DELETING:
                return Response(
                    {'detail': 'App deletion is already in progress.'},
                    status=status.HTTP_409_CONFLICT,
                )

            app.status = App.Status.DELETING
            app.save(update_fields=['status', 'updated_at'])
            cluster = app.cluster
            namespace = app.namespace
            name = app.name

        apps_v1 = get_apps_v1_client(cluster)
        error_response = None

        try:
            apps_v1.delete_namespaced_deployment(name, namespace, _request_timeout=K8S_WRITE_TIMEOUT)
        except ApiException as exc:
            if exc.status != 404:
                error_response = api_exception_response(exc)
        except Exception:
            # As in AppListCreateView.post: the delete may have actually gone
            # through even though we never got a response back.
            if not deployment_gone(apps_v1, name, namespace):
                error_response = Response(
                    {'detail': 'Unable to reach the Kubernetes cluster.'},
                    status=status.HTTP_502_BAD_GATEWAY,
                )

        if error_response is not None:
            app.status = App.Status.ACTIVE
            app.save(update_fields=['status', 'updated_at'])
            return error_response

        app.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AppRefreshStatusView(APIView):
    """On-demand version of clusters.tasks.sync_app_status for one App --
    the periodic task only runs every 2 minutes; this lets the dashboard's
    reload button get an answer immediately instead of waiting up to that
    long. Reuses the exact same reconciliation logic (deployment_present +
    optimistic-concurrency update) rather than a separate check.
    """

    @extend_schema(request=None, responses={200: AppRecordSerializer, 403: None, 404: None})
    def post(self, request, pk):
        try:
            app = App.objects.select_related('cluster').get(pk=pk)
        except App.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)

        if app.owner_id != request.user.id and not request.user.is_staff:
            return Response(status=status.HTTP_403_FORBIDDEN)

        # DELETING is a claim held by AppDeleteView -- never touched here,
        # same rule as the periodic sync task.
        if app.status != App.Status.DELETING:
            apps_v1 = get_apps_v1_client(app.cluster)
            present = deployment_present(apps_v1, app.name, app.namespace)
            if present is not None:
                new_status = App.Status.ACTIVE if present else App.Status.MISSING
                if new_status != app.status:
                    rows = App.objects.filter(pk=app.pk, status=app.status).update(
                        status=new_status, updated_at=timezone.now(),
                    )
                    if rows:
                        app.status = new_status

        return Response(AppRecordSerializer(app).data)


class NamespaceDeleteView(APIView):
    """Delete a backend-tracked namespace by its DB id (doc 3.5): delete
    from Kubernetes first, then remove the DB row, so a crash in between
    leaves at worst a DB row for a namespace already gone from k8s — the
    next delete attempt on that id finds k8s already returns 404 and just
    cleans the row up (self-healing, no separate job needed for that half).

    Concurrency (doc 3.6): two DELETEs racing on the same id are serialized
    by claiming the row (status ACTIVE -> DELETING) inside a short locked
    transaction *before* touching Kubernetes, then doing the k8s call
    outside the lock so a slow network round trip never holds a DB lock.
    A request that finds the row already claimed gets 409 Conflict; once
    the row is actually gone, any further request gets 404.

    Known gap (doc 3.6, "reconciliation"): if the backend crashes *after*
    claiming the row (DELETING) but before finishing the k8s call or the
    final DB delete, that row is stuck in DELETING forever — nothing here
    retries it automatically. The fix, if this were production, is a
    periodic reconciliation job that finds rows stuck in DELETING past some
    age and resumes/retries the delete; `updated_at` on the model exists
    specifically to make that check ("how long has this been DELETING?")
    possible later. Not implemented here — out of scope for this exercise.
    """

    @extend_schema(
        responses={
            204: None,
            404: None,
            409: OpenApiResponse(description='Namespace deletion is already in progress.'),
        },
    )
    def delete(self, request, pk):
        with transaction.atomic():
            try:
                namespace = Namespace.objects.select_for_update().get(pk=pk)
            except Namespace.DoesNotExist:
                return Response(status=status.HTTP_404_NOT_FOUND)

            if not user_can_access_namespace(namespace, request.user):
                return Response(status=status.HTTP_403_FORBIDDEN)

            if namespace.status == Namespace.Status.DELETING:
                return Response(
                    {'detail': 'Namespace deletion is already in progress.'},
                    status=status.HTTP_409_CONFLICT,
                )

            namespace.status = Namespace.Status.DELETING
            namespace.save(update_fields=['status', 'updated_at'])
            cluster = namespace.cluster
            ns_name = namespace.name

        try:
            v1 = get_core_v1_client(cluster)
            v1.delete_namespace(ns_name)
        except ApiException as exc:
            if exc.status != 404:
                namespace.status = Namespace.Status.ACTIVE
                namespace.save(update_fields=['status', 'updated_at'])
                return api_exception_response(exc)
            # Already gone from k8s -- fine, fall through and clean up our row.
        except Exception:
            namespace.status = Namespace.Status.ACTIVE
            namespace.save(update_fields=['status', 'updated_at'])
            return Response(
                {'detail': 'Unable to reach the Kubernetes cluster.'},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        namespace.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(
        operation_id='namespace_update_access',
        request=inline_serializer('NamespaceAccessRequest', fields={
            'is_accessible': serializers.BooleanField(required=False),
            'allowed_user_ids': serializers.ListField(child=serializers.IntegerField(), required=False),
        }),
        responses={200: NamespaceRecordSerializer, 403: None, 404: None},
    )
    def patch(self, request, pk):
        """Admin-only: toggle is_accessible and/or replace the
        allowed_users exception list. Deliberately staff-only with no
        ownership fallback -- unlike delete, an owner can never change
        these fields for their own namespace, only an admin can."""
        if not request.user.is_staff:
            return Response(status=status.HTTP_403_FORBIDDEN)

        try:
            namespace = Namespace.objects.get(pk=pk)
        except Namespace.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)

        if 'is_accessible' in request.data:
            namespace.is_accessible = bool(request.data['is_accessible'])
            namespace.save(update_fields=['is_accessible', 'updated_at'])
        if 'allowed_user_ids' in request.data:
            users = get_user_model().objects.filter(pk__in=request.data['allowed_user_ids'])
            namespace.allowed_users.set(users)

        return Response(NamespaceRecordSerializer(namespace).data)

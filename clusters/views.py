import json
import time

from urllib3 import request
from django.db import IntegrityError, transaction
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema, inline_serializer
from kubernetes import client as k8s_client
from kubernetes.client.exceptions import ApiException
from rest_framework import serializers, status, viewsets
from rest_framework.generics import get_object_or_404
from rest_framework.response import Response
from rest_framework.views import APIView

from .k8s_client import get_apps_v1_client, get_core_v1_client
from .models import App, Cluster, Namespace
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
    queryset = Cluster.objects.all()
    serializer_class = ClusterSerializer


def resolve_cluster(request):
    """Pick the Cluster a namespace request should target.

    ?cluster=<id> wins if given, otherwise the cluster flagged is_default,
    otherwise None (get_core_v1_client falls back to the local kubeconfig).
    """
    cluster_id = request.query_params.get('cluster')
    if cluster_id:
        return get_object_or_404(Cluster, pk=cluster_id)
    return Cluster.objects.filter(is_default=True).first()


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
        namespace = Namespace.objects.create(cluster=cluster, name=name, uid=created.metadata.uid)
        return Response(NamespaceRecordSerializer(namespace).data, status=status.HTTP_201_CREATED)


class NamespaceDetailView(APIView):
    """Live read/patch by k8s name — unchanged by the Namespace-model rework.
    Deletion moved to NamespaceDeleteView (DB id, doc 3.5) since it now needs
    to coordinate with the tracked DB row rather than just proxying k8s."""

    @extend_schema(
        parameters=[OpenApiParameter('cluster', int, description='Cluster id; defaults to the cluster flagged is_default.')],
        responses=NamespaceSerializer,
    )
    def get(self, request, name):
        v1 = get_core_v1_client(resolve_cluster(request))
        try:
            ns = v1.read_namespace(name)
        except ApiException as exc:
            return api_exception_response(exc)
        return Response(NamespaceSerializer(namespace_to_dict(ns)).data)

    @extend_schema(
        parameters=[OpenApiParameter('cluster', int, description='Cluster id; defaults to the cluster flagged is_default.')],
        request=NamespaceSerializer,
        responses=NamespaceSerializer,
    )
    def patch(self, request, name):
        serializer = NamespaceSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)

        v1 = get_core_v1_client(resolve_cluster(request))
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
        app = App(cluster=cluster, **serializer.validated_data)
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

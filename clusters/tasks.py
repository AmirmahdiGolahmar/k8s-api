import logging
from itertools import groupby

from celery import shared_task
from django.utils import timezone

from .k8s_client import get_apps_v1_client
from .models import App
from .views import deployment_present

logger = logging.getLogger(__name__)


@shared_task
def sync_app_status():
    """Reconciles App.status against the real Deployment state in
    Kubernetes, on CELERY_BEAT_SCHEDULE. Only ever moves a row between
    ACTIVE and MISSING -- DELETING rows are a claim held by AppDeleteView
    and are never touched here.

    Grouped by cluster so each cluster gets one k8s client for the whole
    batch instead of one per App.
    """
    updated = 0
    apps = App.objects.exclude(status=App.Status.DELETING).select_related('cluster').order_by('cluster_id')

    for _, cluster_apps in groupby(apps, key=lambda app: app.cluster_id):
        cluster_apps = list(cluster_apps)
        apps_v1 = get_apps_v1_client(cluster_apps[0].cluster)

        for app in cluster_apps:
            present = deployment_present(apps_v1, app.name, app.namespace)
            if present is None:
                # Inconclusive k8s response (network hiccup, timeout) --
                # don't flip status on a check that couldn't actually confirm
                # either way.
                continue

            new_status = App.Status.ACTIVE if present else App.Status.MISSING
            if new_status == app.status:
                continue

            # Optimistic-concurrency guard: only apply if status hasn't
            # changed since we read it above. If AppDeleteView claimed this
            # row (ACTIVE -> DELETING) in the meantime, this condition fails
            # to match and the row is silently left alone.
            rows = App.objects.filter(pk=app.pk, status=app.status).update(
                status=new_status, updated_at=timezone.now(),
            )
            if rows:
                logger.info(
                    'App %s/%s/%s: %s -> %s',
                    app.cluster.name, app.namespace, app.name, app.status, new_status,
                )
                updated += 1

    return updated

import logging

from django.db import IntegrityError, transaction
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, PolymorphicProxySerializer, extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from clusters.models import App

from .models import Backup, BackupSchedule
from .serializers import (
    BackupCreateSerializer,
    BackupListItemSerializer,
    BackupScheduleCreateSerializer,
    BackupScheduleListItemSerializer,
    BackupScheduleSerializer,
    BackupSerializer,
)
from .tasks import run_backup, sweep_stale_backups

logger = logging.getLogger(__name__)


class BackupListCreateView(APIView):
    """GET /backup/?app_id=<id> and POST /backup/ -- the single `/backup`
    route the doc describes, split by HTTP method rather than URL.
    """

    @extend_schema(
        parameters=[OpenApiParameter('app_id', int, required=True)],
        responses=PolymorphicProxySerializer(
            component_name='BackupOrScheduleListItem',
            serializers=[BackupListItemSerializer, BackupScheduleListItemSerializer],
            resource_type_field_name='type',
            many=True,
        ),
    )
    def get(self, request):
        app_id = request.query_params.get('app_id')
        if not app_id:
            return Response(
                {'detail': 'app_id query parameter is required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # Cheap inline safety net: a backup stuck past the stale threshold
        # shows as failed here even if the periodic sweep task never ran.
        sweep_stale_backups()

        # Merges two different model types (one-off runs + recurring
        # definitions) into a single feed for the app, newest first. Each
        # item's `type` field (see serializers.py) tells them apart.
        backups = Backup.objects.filter(app_id=app_id)
        schedules = BackupSchedule.objects.filter(app_id=app_id)
        items = [(b.created_at, BackupListItemSerializer(b).data) for b in backups]
        items += [(s.created_at, BackupScheduleListItemSerializer(s).data) for s in schedules]
        items.sort(key=lambda pair: pair[0], reverse=True)

        return Response([data for _, data in items])

    @extend_schema(
        request=PolymorphicProxySerializer(
            component_name='BackupOrScheduleCreateRequest',
            serializers=[BackupCreateSerializer, BackupScheduleCreateSerializer],
            resource_type_field_name=None,
        ),
        responses={
            202: inline_serializer('BackupCreateResponse', fields={
                'backup_id': serializers.CharField(),
                'status': serializers.CharField(),
            }),
            201: BackupScheduleSerializer,
        },
    )
    def post(self, request):
        # Same /backup route for both: presence of `schedule` in the body is
        # what distinguishes a one-off backup from a recurring one (doc:
        # "Backup دورهای" reuses the same POST /backup endpoint).
        if 'schedule' in request.data:
            return self._create_schedule(request)
        return self._create_immediate_backup(request)

    def _create_immediate_backup(self, request):
        serializer = BackupCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        app = serializer.validated_data['app']

        if app.status != App.Status.ACTIVE:
            return Response(
                {'detail': f'App "{app.name}" is not active (status: {app.status}).'},
                status=status.HTTP_409_CONFLICT,
            )

        try:
            backup = Backup.objects.create(**serializer.validated_data)
        except IntegrityError:
            # public_id collision on the unique constraint -- astronomically
            # unlikely (16.7M keyspace via secrets.token_hex(3)), but retry
            # once with a freshly generated id rather than 500ing.
            backup = Backup.objects.create(**serializer.validated_data)

        def enqueue():
            try:
                run_backup.delay(backup.id)
            except Exception:
                # Broker/result backend unreachable (Redis down, etc). The
                # doc treats "worker never picks this up" as a first-class
                # failure mode -- record it as failed immediately rather
                # than leaving the row stuck pending with nothing that will
                # ever process it, and rather than letting this exception
                # escape as an unhandled 500.
                logger.exception('Could not enqueue backup %s -- broker unreachable.', backup.public_id)
                Backup.objects.filter(pk=backup.pk).update(
                    status=Backup.Status.FAILED,
                    error_message='Could not reach the task queue (Celery broker unavailable). The backup was never started.',
                    finished_at=timezone.now(),
                )

        # Only enqueue once the row is actually committed, so the worker can
        # never race ahead of the request and find a Backup that isn't there yet.
        transaction.on_commit(enqueue)

        backup.refresh_from_db()
        return Response(
            {'backup_id': backup.public_id, 'status': backup.status},
            status=status.HTTP_202_ACCEPTED,
        )

    def _create_schedule(self, request):
        serializer = BackupScheduleCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        app = serializer.validated_data['app']

        if app.status != App.Status.ACTIVE:
            return Response(
                {'detail': f'App "{app.name}" is not active (status: {app.status}).'},
                status=status.HTTP_409_CONFLICT,
            )

        try:
            schedule = BackupSchedule.objects.create(**serializer.validated_data)
        except IntegrityError:
            schedule = BackupSchedule.objects.create(**serializer.validated_data)

        # Nothing to enqueue here -- unlike an immediate backup, a schedule
        # doesn't run anything itself. run_due_backup_schedules (Celery Beat,
        # every minute) is what notices it and creates each run's own Backup.
        return Response(BackupScheduleSerializer(schedule).data, status=status.HTTP_201_CREATED)


class BackupDetailView(APIView):
    """GET /backup/{id}/ -- looked up by the public id from the list/create
    endpoints, which may be either a one-off backup (bkp_...) or a recurring
    schedule (sch_...); whichever it turns out to be determines the response
    shape (BackupSerializer vs BackupScheduleSerializer)."""

    @extend_schema(
        responses={
            200: PolymorphicProxySerializer(
                component_name='BackupOrSchedule',
                serializers=[BackupSerializer, BackupScheduleSerializer],
                resource_type_field_name='type',
            ),
            404: None,
        },
    )
    def get(self, request, backup_id):
        sweep_stale_backups()

        backup = Backup.objects.filter(public_id=backup_id).first()
        if backup is not None:
            return Response(BackupSerializer(backup).data)

        schedule = BackupSchedule.objects.filter(public_id=backup_id).first()
        if schedule is not None:
            return Response(BackupScheduleSerializer(schedule).data)

        return Response(status=status.HTTP_404_NOT_FOUND)

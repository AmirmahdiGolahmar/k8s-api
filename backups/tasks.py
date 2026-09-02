import json
import logging
import time
from pathlib import Path

from celery import shared_task
from croniter import croniter
from django.conf import settings
from django.utils import timezone
from kubernetes.stream import stream

from clusters.k8s_client import get_core_v1_client

from .metrics import BACKUP_DURATION_SECONDS
from .models import Backup, BackupSchedule

logger = logging.getLogger(__name__)

# The kubectl-exec websocket protocol's error channel: after the command
# finishes, it carries a JSON Status object (Success, or Failure + message)
# for the *exec'd command*, since exiting non-zero doesn't raise on its own.
ERROR_CHANNEL = 3


class PermanentBackupError(Exception):
    """A backup failure where retrying the task would not help (bad
    source_path, no running Pod, ...) -- fail immediately, don't retry."""


def _select_pod(app):
    """Pick a running Pod belonging to `app`. Deliberately done inside the
    task (not the view) so the HTTP request never waits on a k8s API call,
    and so the pod is as fresh as possible at execution time.
    """
    v1 = get_core_v1_client(app.cluster)
    pods = v1.list_namespaced_pod(app.namespace, label_selector=f'app={app.name}', _request_timeout=10)
    running = [p for p in pods.items if p.status.phase == 'Running']
    if not running:
        raise PermanentBackupError(
            f'No running Pod found for app "{app.name}" in namespace "{app.namespace}".'
        )
    pod = running[0]
    container = pod.spec.containers[0].name
    return pod.metadata.name, container


def _dest_path(backup):
    day = timezone.now().strftime('%Y-%m-%d')
    directory = Path(settings.BACKUP_DIR) / str(backup.app_id) / day
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f'{backup.public_id}.tar.gz'


def _copy_from_pod(cluster, namespace, pod_name, container, source_path, dest):
    """Stream `tar czf - <source_path>` out of the Pod via exec and write it
    straight to `dest` on the worker's local disk -- the same mechanism
    `kubectl cp` uses under the hood, since the kubernetes client has no
    higher-level copy API.
    """
    source = Path(source_path)
    exec_command = ['tar', 'czf', '-', '-C', str(source.parent), source.name]

    v1 = get_core_v1_client(cluster)
    resp = stream(
        v1.connect_get_namespaced_pod_exec, pod_name, namespace,
        container=container, command=exec_command,
        stderr=True, stdin=False, stdout=True, tty=False,
        _preload_content=False, binary=True,
    )

    stderr_chunks = []
    try:
        with open(dest, 'wb') as f:
            while resp.is_open():
                resp.update(timeout=1)
                if resp.peek_stdout():
                    f.write(resp.read_stdout())
                if resp.peek_stderr():
                    stderr_chunks.append(resp.read_stderr())
        outcome = json.loads(resp.read_channel(ERROR_CHANNEL) or b'{}')
    finally:
        resp.close()

    if outcome.get('status') != 'Success':
        dest.unlink(missing_ok=True)
        message = outcome.get('message') or b''.join(stderr_chunks).decode('utf-8', 'replace')
        raise PermanentBackupError(
            f'Failed to read "{source_path}" from pod "{pod_name}": {message or "tar exited with a non-zero status"}'
        )


def _mark_failed(backup, message):
    logger.error('Backup %s failed: %s', backup.public_id, message)
    backup.status = Backup.Status.FAILED
    backup.error_message = message
    backup.finished_at = timezone.now()
    backup.save(update_fields=['status', 'error_message', 'finished_at'])


@shared_task(bind=True, max_retries=2, default_retry_delay=10)
def run_backup(self, backup_id):
    backup = Backup.objects.select_related('app', 'app__cluster').get(pk=backup_id)
    backup.status = Backup.Status.RUNNING
    backup.started_at = timezone.now()
    backup.save(update_fields=['status', 'started_at'])

    # Excludes time spent on transient-error retries (each retry is a fresh
    # task invocation with its own timer) -- this measures one successful
    # (or finally-failed) attempt, not the whole retry sequence.
    attempt_start = time.monotonic()
    try:
        pod_name, container = _select_pod(backup.app)
        dest = _dest_path(backup)
        _copy_from_pod(backup.app.cluster, backup.app.namespace, pod_name, container, backup.source_path, dest)
    except PermanentBackupError as exc:
        # Retrying a bad path or a missing Pod selector wouldn't help.
        BACKUP_DURATION_SECONDS.labels(app=backup.app.name, status='failed').observe(time.monotonic() - attempt_start)
        _mark_failed(backup, str(exc))
        return
    except Exception as exc:
        # Anything else (k8s API unreachable, timeout, ...) is presumed
        # transient -- bounded retry, then give up and record it as failed
        # rather than leaving the row stuck in "running" forever.
        if self.request.retries < self.max_retries:
            logger.warning('Backup %s hit a transient error, retrying: %s', backup.public_id, exc)
            raise self.retry(exc=exc)
        BACKUP_DURATION_SECONDS.labels(app=backup.app.name, status='failed').observe(time.monotonic() - attempt_start)
        _mark_failed(backup, f'Giving up after {self.max_retries} retries: {exc}')
        return

    BACKUP_DURATION_SECONDS.labels(app=backup.app.name, status='completed').observe(time.monotonic() - attempt_start)
    backup.pod_name = pod_name
    backup.status = Backup.Status.COMPLETED
    backup.file_path = str(dest)
    backup.finished_at = timezone.now()
    backup.save(update_fields=['pod_name', 'status', 'file_path', 'finished_at'])


def sweep_stale_backups():
    """Fail any backup still pending/running past BACKUP_STALE_TIMEOUT.

    Covers the doc's "stuck for 24h" scenarios (worker down, queue stuck,
    task died without updating state): rather than leaving those rows
    pending forever, flip them to failed with a log entry. Called both from
    the periodic Celery Beat task below and inline from the status/list
    views, so a stuck backup reads as failed even if Beat isn't running.
    """
    cutoff = timezone.now() - settings.BACKUP_STALE_TIMEOUT
    stale_ids = list(
        Backup.objects.filter(
            status__in=[Backup.Status.PENDING, Backup.Status.RUNNING],
            created_at__lt=cutoff,
        ).values_list('id', flat=True)
    )
    if not stale_ids:
        return 0
    for backup_id in stale_ids:
        logger.error('Backup id=%s stuck past the stale threshold -- marking failed.', backup_id)
    return Backup.objects.filter(id__in=stale_ids).update(
        status=Backup.Status.FAILED,
        error_message=(
            'Backup timed out: still pending/running after the stale threshold. '
            'Check that a Celery worker is up and the queue is not stuck.'
        ),
        finished_at=timezone.now(),
    )


@shared_task
def mark_stale_backups():
    return sweep_stale_backups()


def _enqueue_scheduled_run(schedule):
    """Create one independent Backup for a firing BackupSchedule -- the same
    creation the immediate POST /backup path does, so it shows up in
    GET /backup?app_id=... and GET /backup/{backup_id}/ exactly like any
    other backup (doc requirement)."""
    backup = Backup.objects.create(app=schedule.app, source_path=schedule.source_path)
    try:
        run_backup.delay(backup.id)
    except Exception:
        logger.exception(
            'Could not enqueue scheduled backup %s (schedule %s) -- broker unreachable.',
            backup.public_id, schedule.public_id,
        )
        Backup.objects.filter(pk=backup.pk).update(
            status=Backup.Status.FAILED,
            error_message='Could not reach the task queue (Celery broker unavailable). The scheduled backup was never started.',
            finished_at=timezone.now(),
        )
    return backup


@shared_task
def run_due_backup_schedules():
    """Ticks every minute (CELERY_BEAT_SCHEDULE). For each enabled
    BackupSchedule whose cron expression matches the current minute, create
    a fresh Backup and run it -- doc: "هر بار که این Backup زمان‌بندی‌شده
    اجرا می‌شود، باید یک Backup مستقل جدید بسازد."
    """
    now = timezone.now()
    current_minute = now.replace(second=0, microsecond=0)
    triggered = 0

    for schedule in BackupSchedule.objects.filter(enabled=True):
        if not croniter.match(schedule.cron_expression, now):
            continue
        # Guards against firing twice for the same minute -- an overlapping
        # Beat tick or a task redelivery would otherwise create a duplicate
        # Backup for the same scheduled slot.
        if schedule.last_triggered_at and schedule.last_triggered_at.replace(second=0, microsecond=0) == current_minute:
            continue

        backup = _enqueue_scheduled_run(schedule)
        BackupSchedule.objects.filter(pk=schedule.pk).update(last_triggered_at=now)
        logger.info('Schedule %s fired -- created backup %s.', schedule.public_id, backup.public_id)
        triggered += 1

    return triggered

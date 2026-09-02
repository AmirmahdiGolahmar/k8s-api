import os
import shutil
from pathlib import Path

from celery import Celery
from celery.signals import worker_init, worker_process_shutdown

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'k8s_api.settings')

app = Celery('k8s_api')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()


@worker_init.connect
def _start_metrics_server(**kwargs):
    """Exposes backups.metrics.BACKUP_DURATION_SECONDS (and anything else
    recorded via prometheus_client in this worker) on its own /metrics.

    Celery's default pool runs the actual tasks in several forked child
    processes, so a plain in-process Histogram wouldn't see every task's
    .observe() call -- only whichever child made it. PROMETHEUS_MULTIPROC_DIR
    switches prometheus_client to its file-backed multiprocess mode instead
    (each process writes to its own file there); worker_init fires once, in
    the main process, before the pool forks, making it the right place to
    start the single HTTP server that aggregates across all of them at
    scrape time. If the env var isn't set (e.g. `beat`, which imports this
    module too but never fires worker_init), this is a no-op.
    """
    multiproc_dir = os.environ.get('PROMETHEUS_MULTIPROC_DIR')
    if not multiproc_dir:
        return

    from prometheus_client import CollectorRegistry, multiprocess, start_http_server

    # Wipe stale files from a previous container life -- same dir, PIDs can
    # be reused, and MultiProcessCollector aggregates every file it finds
    # regardless of whether that process is still alive.
    shutil.rmtree(multiproc_dir, ignore_errors=True)
    Path(multiproc_dir).mkdir(parents=True, exist_ok=True)

    registry = CollectorRegistry()
    multiprocess.MultiProcessCollector(registry)
    start_http_server(int(os.environ.get('PROMETHEUS_WORKER_METRICS_PORT', 9808)), registry=registry)


@worker_process_shutdown.connect
def _cleanup_metrics(pid, **kwargs):
    """Removes the exiting child's per-pid metrics file so a dead process's
    last values don't linger forever (relevant with worker restarts /
    --max-tasks-per-child)."""
    if os.environ.get('PROMETHEUS_MULTIPROC_DIR'):
        from prometheus_client import multiprocess
        multiprocess.mark_process_dead(pid)

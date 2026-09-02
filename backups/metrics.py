from prometheus_client import Histogram

# How long backups.tasks.run_backup takes end to end -- Pod selection
# through the finished (or failed) tar stream. Buckets span a few seconds
# (small config dirs) up to an hour (large volumes), since source_path size
# varies per app.
BACKUP_DURATION_SECONDS = Histogram(
    'backup_duration_seconds',
    'Time spent running a backup, from Pod selection to completion or failure.',
    ['app', 'status'],
    buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1200, 1800, 3600),
)

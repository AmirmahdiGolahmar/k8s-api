import secrets

from django.db import models


def generate_public_id():
    return f'bkp_{secrets.token_hex(3)}'


def generate_schedule_public_id():
    return f'sch_{secrets.token_hex(3)}'


class Backup(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        RUNNING = 'running', 'Running'
        COMPLETED = 'completed', 'Completed'
        FAILED = 'failed', 'Failed'

    # The id exposed to API clients (doc: "bkp_8f31c2"). The DB pk stays a
    # plain int for FK/indexing; this is just the public-facing lookup key.
    public_id = models.CharField(max_length=20, unique=True, default=generate_public_id, editable=False)
    app = models.ForeignKey('clusters.App', on_delete=models.CASCADE, related_name='backups')
    source_path = models.CharField(max_length=500)
    # Filled in by the worker once it has picked a Pod to read from.
    pod_name = models.CharField(max_length=253, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    file_path = models.CharField(max_length=500, blank=True)
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.public_id} ({self.status})'


class BackupSchedule(models.Model):
    """A recurring backup definition (doc: "Backup دورهای"). Distinct from
    Backup, which represents one run -- a schedule fires repeatedly and
    creates a fresh, independent Backup row (via run_backup, same as an
    immediate POST) every time its cron expression matches.
    """

    public_id = models.CharField(max_length=20, unique=True, default=generate_schedule_public_id, editable=False)
    app = models.ForeignKey('clusters.App', on_delete=models.CASCADE, related_name='backup_schedules')
    source_path = models.CharField(max_length=500)
    # Standard 5-field cron: minute hour day-of-month month day-of-week.
    cron_expression = models.CharField(max_length=100)
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    # Guards against firing twice for the same minute (e.g. an overlapping
    # Beat tick or task redelivery) -- see backups.tasks.run_due_backup_schedules.
    last_triggered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.public_id} ({self.cron_expression})'

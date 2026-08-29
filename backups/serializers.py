from croniter import CroniterBadCronError, croniter
from rest_framework import serializers

from clusters.models import App

from .models import Backup, BackupSchedule


class BackupCreateSerializer(serializers.Serializer):
    app_id = serializers.PrimaryKeyRelatedField(queryset=App.objects.all(), source='app')
    source_path = serializers.CharField(max_length=500)


class BackupScheduleCreateSerializer(serializers.Serializer):
    app_id = serializers.PrimaryKeyRelatedField(queryset=App.objects.all(), source='app')
    source_path = serializers.CharField(max_length=500)
    schedule = serializers.CharField(source='cron_expression', max_length=100)

    def validate_schedule(self, value):
        # croniter also accepts 6/7-field (seconds/year) variants; the doc's
        # cron format is strictly the 5-field standard (minute hour
        # day-of-month month day-of-week), so field count is checked
        # explicitly rather than relying on croniter's more permissive default.
        if len(value.split()) != 5:
            raise serializers.ValidationError(
                f'"{value}" is not a valid cron expression -- expected exactly 5 fields '
                '(minute hour day-of-month month day-of-week).'
            )
        try:
            croniter(value)
        except CroniterBadCronError as exc:
            raise serializers.ValidationError(f'"{value}" is not a valid cron expression: {exc}') from exc
        return value


class BackupSerializer(serializers.ModelSerializer):
    """Full detail view of a single backup (GET /backup/{id}/ when `id` is a
    bkp_... id)."""

    type = serializers.SerializerMethodField()
    backup_id = serializers.CharField(source='public_id', read_only=True)
    app_id = serializers.IntegerField(read_only=True)

    def get_type(self, instance) -> str:
        return 'backup'

    class Meta:
        model = Backup
        fields = [
            'type', 'backup_id', 'app_id', 'status', 'source_path', 'pod_name',
            'file_path', 'error_message', 'created_at', 'started_at', 'finished_at',
        ]
        read_only_fields = fields


class BackupScheduleSerializer(serializers.ModelSerializer):
    """Full detail view of a schedule -- returned by POST /backup/ when the
    body includes a `schedule` field, and by GET /backup/{id}/ when `id` is
    a sch_... id."""

    type = serializers.SerializerMethodField()
    schedule_id = serializers.CharField(source='public_id', read_only=True)
    app_id = serializers.IntegerField(read_only=True)
    schedule = serializers.CharField(source='cron_expression', read_only=True)
    status = serializers.SerializerMethodField()

    def get_type(self, instance) -> str:
        return 'schedule'

    def get_status(self, instance) -> str:
        return 'active' if instance.enabled else 'disabled'

    class Meta:
        model = BackupSchedule
        fields = ['type', 'schedule_id', 'app_id', 'source_path', 'schedule', 'status']
        read_only_fields = fields


class BackupListItemSerializer(serializers.ModelSerializer):
    """Minimal shape for one Backup row in GET /backup?app_id=... — doc
    requires at least backup_id + status per item; `type` disambiguates it
    from a BackupScheduleListItemSerializer entry in the same merged list."""

    type = serializers.SerializerMethodField()
    backup_id = serializers.CharField(source='public_id', read_only=True)

    def get_type(self, instance) -> str:
        return 'backup'

    class Meta:
        model = Backup
        fields = ['type', 'backup_id', 'status']
        read_only_fields = fields


class BackupScheduleListItemSerializer(serializers.ModelSerializer):
    """Minimal shape for one BackupSchedule row in GET /backup?app_id=...
    -- merged into the same array as immediate-backup entries."""

    type = serializers.SerializerMethodField()
    schedule_id = serializers.CharField(source='public_id', read_only=True)
    schedule = serializers.CharField(source='cron_expression', read_only=True)
    status = serializers.SerializerMethodField()

    def get_type(self, instance) -> str:
        return 'schedule'

    def get_status(self, instance) -> str:
        return 'active' if instance.enabled else 'disabled'

    class Meta:
        model = BackupSchedule
        fields = ['type', 'schedule_id', 'schedule', 'status']
        read_only_fields = fields

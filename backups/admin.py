from django.contrib import admin

from .models import Backup, BackupSchedule


@admin.register(Backup)
class BackupAdmin(admin.ModelAdmin):
    list_display = ['public_id', 'app', 'status', 'created_at', 'finished_at']
    list_filter = ['status']
    readonly_fields = [f.name for f in Backup._meta.fields]


@admin.register(BackupSchedule)
class BackupScheduleAdmin(admin.ModelAdmin):
    list_display = ['public_id', 'app', 'cron_expression', 'enabled', 'last_triggered_at']
    list_filter = ['enabled']
    readonly_fields = ['public_id', 'created_at', 'last_triggered_at']

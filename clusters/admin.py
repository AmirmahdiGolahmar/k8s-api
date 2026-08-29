from django.contrib import admin

from .models import App, Cluster


@admin.register(Cluster)
class ClusterAdmin(admin.ModelAdmin):
    list_display = ['name', 'api_server', 'is_default', 'created_at']


@admin.register(App)
class AppAdmin(admin.ModelAdmin):
    list_display = ['name', 'cluster', 'namespace', 'image', 'replicas', 'status', 'created_at']

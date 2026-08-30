from django.contrib import admin

from .models import App, Cluster, Namespace


@admin.register(Cluster)
class ClusterAdmin(admin.ModelAdmin):
    list_display = ['name', 'api_server', 'is_default', 'created_by', 'is_accessible', 'created_at']


@admin.register(Namespace)
class NamespaceAdmin(admin.ModelAdmin):
    list_display = ['name', 'cluster', 'owner', 'status', 'is_accessible', 'created_at']


@admin.register(App)
class AppAdmin(admin.ModelAdmin):
    list_display = ['name', 'cluster', 'namespace', 'owner', 'image', 'replicas', 'status', 'created_at']

from django.urls import path

from .views import BackupDetailView, BackupListCreateView

urlpatterns = [
    path('', BackupListCreateView.as_view(), name='backup-list-create'),
    path('<str:backup_id>/', BackupDetailView.as_view(), name='backup-detail'),
]

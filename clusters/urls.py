from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    AppDeleteView,
    AppListCreateView,
    AppRefreshStatusView,
    ClusterViewSet,
    NamespaceDeleteView,
    NamespaceDetailView,
    NamespaceListCreateView,
)

router = DefaultRouter()
router.register('cluster', ClusterViewSet, basename='cluster')

urlpatterns = [
    path('', include(router.urls)),
    path('app/', AppListCreateView.as_view(), name='app-list'),
    path('app/<int:pk>/', AppDeleteView.as_view(), name='app-delete'),
    path('app/<int:pk>/refresh/', AppRefreshStatusView.as_view(), name='app-refresh'),
    path('namespace/', NamespaceListCreateView.as_view(), name='namespace-list'),
    # int:pk (DB id, delete) is listed before str:name (live k8s read/patch)
    # so a numeric path always resolves as an id, not a namespace name — see
    # NamespaceDeleteView's docstring for the resulting known ambiguity with
    # purely-numeric namespace names.
    path('namespace/<int:pk>/', NamespaceDeleteView.as_view(), name='namespace-delete'),
    path('namespace/<str:name>/', NamespaceDetailView.as_view(), name='namespace-detail'),
]

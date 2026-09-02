"""
URL configuration for k8s_api project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView


def healthz(request):
    """k8s liveness/readiness probe target. Deliberately checks nothing but
    that the process can respond -- a DB/Redis check here would make the
    pod restart on a transient dependency blip instead of just that request
    failing, which is worse."""
    return JsonResponse({'status': 'ok'})


urlpatterns = [
    path('admin/', admin.site.urls),
    path('auth/', include('accounts.urls')),
    path('', include('clusters.urls')),
    path('backup/', include('backups.urls')),
    path('schema/', SpectacularAPIView.as_view(), name='schema'),
    path('docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
    path('healthz/', healthz, name='healthz'),
    # django_prometheus.urls defines its own route as "metrics" (no leading
    # path of its own) -- including it under a 'metrics/' prefix here would
    # produce /metrics/metrics, not /metrics. Include it at the root instead.
    # No auth of its own -- deliberately left out of manifests/06-ingress.yaml
    # so it's unreachable from the public internet; only Prometheus, scraping
    # the ClusterIP Service from inside the cluster, ever hits this.
    path('', include('django_prometheus.urls')),
]

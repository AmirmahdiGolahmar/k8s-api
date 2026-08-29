import os

from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'k8s_api.settings')

app = Celery('k8s_api')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()

from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models

from .k8s_client import resolve_api_server

namespace_name_validator = RegexValidator(
    regex=r'^[a-z0-9]([-a-z0-9]*[a-z0-9])?$',
    message='Name must be a valid Kubernetes namespace name (lowercase alphanumeric and "-", must start/end with alphanumeric).',
)


class Cluster(models.Model):
    name = models.CharField(max_length=100, unique=True)
    description = models.CharField(max_length=255, blank=True)
    api_server = models.URLField(blank=True)
    # Raw kubeconfig YAML for this cluster. If left blank, the local default
    # kubeconfig (~/.kube/config or in-cluster config) is used instead.
    kubeconfig = models.TextField(blank=True)
    is_default = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if self.is_default:
            Cluster.objects.exclude(pk=self.pk).filter(is_default=True).update(is_default=False)
        if not self.api_server:
            self.api_server = resolve_api_server(self)
        super().save(*args, **kwargs)


class Namespace(models.Model):
    """A namespace this backend created and is tracking. Source of truth for
    what "our" namespaces are — a namespace created directly in Kubernetes by
    someone else never gets a row here (see doc section 3.4).

    `status` is the intermediate DB state used to serialize concurrent
    DELETEs (doc section 3.6): a row is claimed (ACTIVE -> DELETING) before
    the Kubernetes call is made, so a second concurrent delete request sees
    the claim and backs off with 409 instead of racing the first request.
    A row stuck in DELETING (backend crashed mid-delete) is a known gap —
    see NamespaceDeleteView's docstring for how it's meant to be reconciled.
    """

    class Status(models.TextChoices):
        ACTIVE = 'active', 'Active'
        DELETING = 'deleting', 'Deleting'

    cluster = models.ForeignKey(Cluster, on_delete=models.CASCADE, related_name='namespaces')
    # Nullable so pre-existing rows (created before ownership existed)
    # don't break the migration; null == nobody's, only staff can see/manage
    # those. Every row created through NamespaceListCreateView.post sets
    # this to request.user.
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='owned_namespaces', null=True, blank=True,
    )
    name = models.CharField(max_length=63, validators=[namespace_name_validator])
    uid = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        unique_together = ('cluster', 'name')

    def __str__(self):
        return f'{self.cluster.name}/{self.name}'


class App(models.Model):
    """A backend-tracked Deployment+Service pair. Source of truth mirrors
    Namespace: a row exists only once both k8s objects are confirmed created
    (see AppListCreateView), and delete uses the same claim-then-delete
    pattern (see AppDeleteView) to serialize concurrent deletes.
    """

    class Status(models.TextChoices):
        ACTIVE = 'active', 'Active'
        DELETING = 'deleting', 'Deleting'
        # Set by clusters.tasks.sync_app_status when the backing Deployment
        # is gone from Kubernetes but nothing went through AppDeleteView to
        # remove this row (deleted directly via kubectl, etc). Flipped back
        # to ACTIVE by the same task if the Deployment reappears.
        MISSING = 'missing', 'Missing'

    cluster = models.ForeignKey(Cluster, on_delete=models.CASCADE, related_name='apps')
    # Same nullable-for-legacy-rows reasoning as Namespace.owner above.
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='owned_apps', null=True, blank=True,
    )
    name = models.CharField(max_length=63, validators=[namespace_name_validator])
    # Plain string, not a FK to Namespace: an App should be deployable into
    # any existing k8s namespace, not just ones this backend happens to track.
    namespace = models.CharField(max_length=63, validators=[namespace_name_validator])
    image = models.CharField(max_length=255, default='nginx:latest')
    replicas = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        unique_together = ('cluster', 'namespace', 'name')

    def __str__(self):
        return f'{self.cluster.name}/{self.namespace}/{self.name}'

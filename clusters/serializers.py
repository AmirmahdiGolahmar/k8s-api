from rest_framework import serializers

from .models import App, Cluster, Namespace


class ClusterSerializer(serializers.ModelSerializer):
    created_by_username = serializers.CharField(source='created_by.username', read_only=True, allow_null=True)

    class Meta:
        model = Cluster
        fields = [
            'id', 'name', 'description', 'api_server', 'kubeconfig', 'is_default',
            'is_accessible', 'allowed_users', 'created_by', 'created_by_username',
            'created_at', 'updated_at',
        ]
        extra_kwargs = {
            'kubeconfig': {'write_only': True, 'required': False},
            'created_by': {'read_only': True},
        }


class NamespaceSerializer(serializers.Serializer):
    """Live representation read straight from the Kubernetes API. Used by
    NamespaceDetailView (GET/PATCH by name) — untouched by the list/create
    rework, which now goes through NamespaceRecordSerializer instead."""

    name = serializers.RegexField(
        regex=r'^[a-z0-9]([-a-z0-9]*[a-z0-9])?$', max_length=63,
    )
    uid = serializers.CharField(read_only=True)
    status = serializers.CharField(read_only=True)
    labels = serializers.DictField(child=serializers.CharField(), required=False)
    annotations = serializers.DictField(child=serializers.CharField(), required=False)
    created_at = serializers.DateTimeField(read_only=True)


class NamespaceRecordSerializer(serializers.ModelSerializer):
    """Backend-tracked namespace record — the DB row, not a k8s live-read.
    This is the Source of Truth for list/create (doc section 3.4).

    is_accessible/allowed_users are read-only here on purpose: this
    serializer is also used for a regular user's own create/list, and
    those two fields are admin-only to change (see NamespaceAccessView)."""

    owner_username = serializers.CharField(source='owner.username', read_only=True, allow_null=True)

    class Meta:
        model = Namespace
        fields = ['id', 'name', 'owner', 'owner_username', 'is_accessible', 'allowed_users']
        read_only_fields = ['id', 'owner', 'owner_username', 'is_accessible', 'allowed_users']


class AppRecordSerializer(serializers.ModelSerializer):
    """Backend-tracked App record — the DB row, source of truth for the
    Deployment+Service pair it represents (doc: mirrors Namespace's pattern)."""

    owner_username = serializers.CharField(source='owner.username', read_only=True, allow_null=True)

    class Meta:
        model = App
        fields = ['id', 'name', 'namespace', 'image', 'replicas', 'status', 'owner', 'owner_username']
        read_only_fields = ['id', 'status', 'owner', 'owner_username']

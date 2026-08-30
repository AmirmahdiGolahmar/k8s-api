from rest_framework import permissions


class IsAdminOrReadOnly(permissions.BasePermission):
    """Any authenticated user can list/retrieve Clusters (to pick one via
    cluster_id when creating a Namespace/App); only admin/staff users can
    add, edit, or delete one -- that's the one place kubeconfigs get
    submitted, and it controls what a regular user's namespace/app CRUD
    can even reach.

    Combine with IsAuthenticated (not standalone): has_permission() returns
    True unconditionally for safe methods, so used alone this would also
    let anonymous requests read the cluster list.
    """

    def has_permission(self, request, view):
        if request.method in permissions.SAFE_METHODS:
            return True
        return bool(request.user and request.user.is_staff)

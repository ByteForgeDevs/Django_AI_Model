"""Permission classes the project supplies itself.

Named so that a rule matching DRF's built-in names cannot recognise them --
which is the point. A project is not obliged to use `IsAuthenticated` by name,
and a rule that only trusts the names it knows reports everyone else.
"""

from rest_framework import permissions


class IsActiveStaffOrReadOnly(permissions.BasePermission):
    """The project-wide default, written locally.

    Not DJA-001. `DEFAULT_PERMISSION_CLASSES` names this class rather than one
    of DRF's, so a rule looking for `IsAuthenticated` in the setting finds
    nothing it recognises and must not conclude the project is open.
    """

    def has_permission(self, request, view) -> bool:
        if request.method in permissions.SAFE_METHODS:
            return bool(request.user and request.user.is_authenticated)
        return bool(request.user and request.user.is_staff)


class IsOwner(permissions.BasePermission):
    """Object-level ownership, checked where DRF calls it."""

    def has_object_permission(self, request, view, obj) -> bool:
        return obj.owner_id == request.user.id

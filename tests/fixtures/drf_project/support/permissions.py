"""Object-level permissions for the DRF recall fixture.

`IsOwner` exists so that DJA-005 has something to skip. A `get_object`
override only loses something when an object-level permission was configured
in the first place -- on a view whose permissions are all class-level, not
calling the hook costs nothing, and the rule is right to say nothing.
"""

from rest_framework.permissions import BasePermission


class IsOwner(BasePermission):
    """Allows access only to the user named on the object."""

    def has_object_permission(self, request, view, obj):
        return obj.owner_id == request.user.id

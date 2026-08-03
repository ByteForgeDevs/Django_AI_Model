"""The API surface: what the application actually exposes over HTTP.

The model graph says what data exists and who owns it. This package says which
of it leaves the process, under what permissions, and through which queryset —
and an authorization finding needs both halves. Knowing that `Check` belongs
to a user via `project__owner` is only useful once you can also see the
viewset that returns `Check.objects.all()` to anyone who asks.
"""

from __future__ import annotations

from djaudit.api.discovery import ApiSurface, build_api_surface
from djaudit.api.serializers import SerializerField, SerializerNode

__all__ = [
    "ApiSurface",
    "SerializerField",
    "SerializerNode",
    "build_api_surface",
]

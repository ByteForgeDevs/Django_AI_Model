"""Fixture urlconf. Never imported -- djaudit only parses it.

PLANTED DEFECT: the admin sits at /admin/ (DJS-026) with nothing in front of
it -- no lockout, no second factor, no honeypot -- so the site's logs are a
permanent wash of credential stuffing and a real attempt looks like all of it.
"""

from django.contrib import admin
from django.urls import path

urlpatterns = [
    path("admin/", admin.site.urls),
]

"""Fixture urlconf. Never imported -- djaudit only parses it.

Control for DJS-026: the admin is at the default path and must not be
reported, because INSTALLED_APPS carries django-axes.
"""

from django.contrib import admin
from django.urls import path

urlpatterns = [
    path("admin/", admin.site.urls),
]

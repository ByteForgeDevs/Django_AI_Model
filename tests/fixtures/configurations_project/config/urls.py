"""Fixture urlconf. Never imported -- djaudit only parses it.

Control for DJS-026: the admin is moved off the default path.
"""

from django.contrib import admin
from django.urls import path

urlpatterns = [
    path("back-office/", admin.site.urls),
]

"""Fixture urlconf. Never imported -- djaudit only parses it.

The control for DJS-026: the admin is moved off the default path, so the rule
must stay silent. api_project is the other half of the control: it leaves the
admin where it is and installs django-axes instead, which is the better answer
and suppresses the rule on its own.
"""

from django.contrib import admin
from django.urls import path

urlpatterns = [
    path("staff-console-8f21/", admin.site.urls),
]

"""Fixture urlconf. Never imported -- djaudit only parses it.

Control for DJS-026: the admin is not routed at all. `django.contrib.admin` is
in INSTALLED_APPS because a third-party package needs its templates, which is
common and is not the same thing as exposing the login form.
"""

from django.urls import path

from . import views

urlpatterns = [
    path("healthz/", views.healthz),
]

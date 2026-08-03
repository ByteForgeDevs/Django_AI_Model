"""Fixture views. Never imported -- djaudit only parses it."""

from django.http import HttpResponse


def healthz(request):
    return HttpResponse("ok")

"""Shared base settings.

PLANTED DEFECT: DEBUG is enabled here and no production module turns it off,
so this should be reported at high/firm.
"""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent

# PLANTED DEFECT: the throwaway key startproject writes, which is DJS-003.
SECRET_KEY = "django-insecure-fixture-key-not-a-real-secret"

DEBUG = True

ALLOWED_HOSTS = ["example.test"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "app",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "fixture",
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
STATIC_URL = "static/"
USE_TZ = True

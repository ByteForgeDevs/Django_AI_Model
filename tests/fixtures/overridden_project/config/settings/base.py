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
        # Control for DJS-021: connections are reused, so the rule must not
        # report this alias.
        "CONN_MAX_AGE": 60,
        "CONN_HEALTH_CHECKS": True,
        # Control for DJS-022: a networked Postgres connection that verifies
        # the server it reached. HOST is spelled out rather than left empty, so
        # this exercises the reported path and not the Unix socket shortcut.
        "HOST": "db.internal.example",
        "OPTIONS": {"sslmode": "verify-full", "sslrootcert": "/etc/ssl/certs/ca.pem"},
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
STATIC_URL = "static/"
USE_TZ = True

# Control for DJS-020: the four validators startproject generates. A project
# that keeps them must never be reported.
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

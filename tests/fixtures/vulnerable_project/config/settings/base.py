"""Shared base settings.

PLANTED DEFECT: DEBUG is enabled here and no production module turns it off,
so this should be reported at high/firm.
"""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent

# PLANTED DEFECT: a strong key, committed to the repository, which is DJS-002.
# Random and fixture-only -- it signs nothing and never has. It is deliberately
# strong so that DJS-003 does not claim it: overridden_project carries the weak
# key, and between them both rules keep a recall case of their own.
SECRET_KEY = "*x3t(5n^s*5!^3p6ideyjx%u!wwl@bdy$sf-jlu&3n$e&!#y*1"

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
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
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

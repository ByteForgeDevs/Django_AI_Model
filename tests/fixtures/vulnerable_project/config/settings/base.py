"""Shared base settings.

PLANTED DEFECT: DEBUG is enabled here and no production module turns it off,
so this should be reported at high/firm.
"""

import os
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
    "corsheaders.middleware.CorsMiddleware",
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
        # PLANTED DEFECT: a database password in the repository, which is
        # DJS-004. The host beside it is unresolvable on purpose: that is what
        # collapses the dict in real projects, and the rule has to see past it.
        "PASSWORD": "fixture-db-password-not-real",
        "HOST": os.environ["FIXTURE_DB_HOST"],
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
STATIC_URL = "static/"
USE_TZ = True

# PLANTED DEFECT: a service credential in the repository, which is DJS-005.
# Matched by name rather than by being known, so the two settings under it are
# controls: one reads like a credential and holds policy, the other holds an
# import path.
STRIPE_SECRET_KEY = "fixture-stripe-value-not-real-0123456789"
PASSWORD_RESET_TIMEOUT = 3600
NOTIFICATION_TOKEN = "app.notifications.TokenBackend"

# PLANTED DEFECT: MD5 put first to speed the test suite up, in the module every
# environment imports (DJS-019). PBKDF2 is kept below it, so logins keep working
# and nothing about the application looks wrong -- which is why this survives.
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.MD5PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]

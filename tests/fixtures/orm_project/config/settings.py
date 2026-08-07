"""Settings for the ORM recall fixture.

Nothing here is a planted defect. The defects in this project are all in
`inventory/`, and they are about queries rather than configuration, so anything
the `DJS` or `DJA` families say about this file is a regression.

Two settings are load-bearing for rules that live elsewhere, and they are set
deliberately rather than by habit.

`REST_FRAMEWORK` configures pagination with **both** a class and a `PAGE_SIZE`,
which is what `DJA-013` wants and is also the precondition for `DJD-003`: a
list endpoint only pages if pagination is really on, and a page of an unordered
query is only unstable because there is a `LIMIT` to make it so. With
pagination off, `DJD-003` would have nothing to say about any of these views.

`DEFAULT_PERMISSION_CLASSES` is `IsAuthenticated`, which keeps the whole `DJA`
authorization family quiet here. That is the point: this fixture measures the
performance families, and a project that trips two families at once cannot show
which one found what.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ["DJANGO_SECRET_KEY"]

DEBUG = False

ALLOWED_HOSTS = ["inventory.example.test"]

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "rest_framework",
    "inventory",
]

MIDDLEWARE = [
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
        "NAME": os.environ["DB_NAME"],
        "USER": os.environ["DB_USER"],
        "PASSWORD": os.environ["DB_PASSWORD"],
        "HOST": os.environ["DB_HOST"],
        "CONN_MAX_AGE": 600,
        "OPTIONS": {"sslmode": "verify-full"},
    }
}

REST_FRAMEWORK = {
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 50,
}

SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True

SESSION_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_SECURE = True

STATIC_URL = "static/"

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]

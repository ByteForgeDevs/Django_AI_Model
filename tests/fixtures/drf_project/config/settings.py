"""Settings for the DRF recall fixture.

Everything outside `REST_FRAMEWORK` is deliberately correct, so that anything
the DJS family says about this project is a regression rather than a finding.
The defects here are all in the API layer, and they are the ones the model
graph and the API surface exist to find.

PLANTED DEFECT: `DEFAULT_PERMISSION_CLASSES` is `AllowAny`. That is DJA-001 on
its own, and it is also the precondition for DJA-002 -- every routed view that
declares no `permission_classes` of its own inherits it, which is how a single
settings line becomes an open API.

PLANTED DEFECT: there is no `DEFAULT_THROTTLE_CLASSES`, which leaves the
anonymous credential endpoint in `support/views.py` free to be asked as fast as
it can answer. That is DJA-015.

Pagination *is* configured, and correctly, with both a class and a PAGE_SIZE.
That makes this project the control for DJA-013: the rule has to stay silent
here while reporting the projects that name a class and no size.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ["DJANGO_SECRET_KEY"]

DEBUG = False

ALLOWED_HOSTS = ["support.example.test"]

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "rest_framework",
    "django_filters",
    "axes",
    "support",
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

# The defect. Every view that does not override this is open to anyone, and
# most of the views in support/views.py do not override it.
REST_FRAMEWORK = {
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_FILTER_BACKENDS": ["django_filters.rest_framework.DjangoFilterBackend"],
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

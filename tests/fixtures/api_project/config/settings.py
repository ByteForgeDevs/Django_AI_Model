"""A browser-facing API in a single settings module.

Two things make this fixture worth having beside the split-settings pair. It is
the shape most Django projects actually have -- one module, no inheritance --
and every rule so far has only ever been exercised against a chain, so this is
where a rule that quietly depends on there being a base module shows up.

PLANTED DEFECT: CORS is opened to every origin *and* credentials are allowed.
django-cors-headers stops sending "*" at that point and echoes the caller's own
origin back with Access-Control-Allow-Credentials: true, so any site a logged-in
user visits can read this API as them. That is DJS-016, and it is the only thing
here that should be reported -- everything else is deliberately correct, which
makes this a control for the rest of the DJS family as much as a recall case.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ["DJANGO_SECRET_KEY"]

DEBUG = False

ALLOWED_HOSTS = ["api.example.test"]

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "corsheaders",
    "rest_framework",
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
        "NAME": os.environ["DB_NAME"],
        "USER": os.environ["DB_USER"],
        "PASSWORD": os.environ["DB_PASSWORD"],
        "HOST": os.environ["DB_HOST"],
    }
}

# The browser front-end sends session cookies, so credentials have to be on.
# Waiving the origin check as well is what turns that into the defect.
CORS_ALLOW_ALL_ORIGINS = True
CORS_ALLOW_CREDENTIALS = True

CSRF_TRUSTED_ORIGINS = ["https://app.example.test"]

SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

SESSION_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_SECURE = True

STATIC_URL = "static/"

# Control for DJS-020: this project is API-only but still stores passwords, so
# it keeps a policy. Appended at the end deliberately -- every other finding in
# this fixture is pinned to a line number.
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]

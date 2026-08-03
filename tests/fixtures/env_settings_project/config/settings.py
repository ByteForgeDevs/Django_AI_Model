"""Twelve-factor settings read through django-environ.

This is the shape the other three fixtures do not have, and the one that
decides whether the tool is usable on modern Django at all: nothing is a
literal, every value arrives from the environment, and the source alone cannot
say what the deployment will do.

The fixture exists to hold two lines that pull against each other. Findings
here must survive the indirection -- a wrong default is still a wrong default
even when it is spelled `env.bool(..., default=True)` -- and they must arrive
at *lower confidence* than the same defect written as a literal, because the
operator may well have set the variable. Equally, the correct twelve-factor
idioms must be silent, and there are more of those here than there are defects.
The controls are the more valuable half: a tool that punishes `env.db()` or a
required-with-no-default secret is a tool that punishes the projects that got
this right, and it gets uninstalled.
"""

from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

# The schema form, which is the idiom django-environ's own README leads with.
# `env("SECURE_SSL_REDIRECT")` below has no inline default and falls back to
# what is declared here -- so a reader who only looks at the call site sees no
# value at all.
env = environ.Env(
    SECURE_SSL_REDIRECT=(bool, True),
    SESSION_COOKIE_SECURE=(bool, True),
)

environ.Env.read_env(BASE_DIR / ".env")

# PLANTED DEFECT: DEBUG defaults to on (DJS-001). The variable is named for
# Django rather than for the deployment, so nothing about a missing
# DJANGO_DEBUG in the production environment looks wrong, and the fallback
# quietly serves the debug page to the internet. Reported, but not at the
# certainty of a literal `DEBUG = True`: the operator may have set it.
DEBUG = env.bool("DJANGO_DEBUG", default=True)

# PLANTED DEFECT: startproject's placeholder key kept as the fallback
# (DJS-002/DJS-003). It is in the repository, it is the documented value of
# every Django tutorial on the internet, and it signs sessions and password
# reset tokens on any host where the variable was forgotten.
SECRET_KEY = env(
    "DJANGO_SECRET_KEY",
    default="django-insecure-6y!8x2q@k#3v",
)

# PLANTED DEFECT: the wildcard as the fallback (DJS-013). Written to make the
# container health check pass, which it does, and Host header validation is off
# everywhere the variable is unset.
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["*"])

# CONTROL, NOT A DEFECT: the schema default is True, so this is on. DJS-006
# must stay silent, and it can only do that by reading the Env() schema above
# rather than the call site -- which is exactly the path this line exists to
# hold open.
SECURE_SSL_REDIRECT = env("SECURE_SSL_REDIRECT")

SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=31536000)
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_CONTENT_TYPE_NOSNIFF = True

# CONTROL, NOT A DEFECT: the same schema path again, for a cookie flag.
SESSION_COOKIE_SECURE = env("SESSION_COOKIE_SECURE")
CSRF_COOKIE_SECURE = env.bool("CSRF_COOKIE_SECURE", default=True)

X_FRAME_OPTIONS = "DENY"

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.staticfiles",
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

# CONTROL, NOT A DEFECT: `env.db()` parses DATABASE_URL into a connection dict.
# Nothing about the credentials, the host or the sslmode is visible here, so
# DJS-004 and DJS-022 must both stay silent. Guessing at a URL the source never
# contains would be a false positive on the single most common way a Django
# project configures its database.
DATABASES = {"default": env.db()}
DATABASES["default"]["CONN_MAX_AGE"] = env.int("CONN_MAX_AGE", default=60)

CACHES = {"default": env.cache(default="locmemcache://")}

# CONTROL, NOT A DEFECT: required with no default. django-environ raises
# ImproperlyConfigured when it is unset, which is the correct way to hold a
# credential, and there is no value here for DJS-005 to read. A rule that fired
# on the *name* alone would punish precisely the projects that got this right.
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD")
EMAIL_HOST = env("EMAIL_HOST", default="smtp.example.test")

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
STATIC_URL = "static/"
USE_TZ = True

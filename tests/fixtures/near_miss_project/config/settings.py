"""Correct settings written in the shapes that look wrong.

There are no defects in this file. Every line is here because a plausible
implementation of some rule fires on it, and every one of those would be a
false positive -- the kind that arrives on a project that did the work, gets
dismissed, and takes the tool's credibility with it.

The recall fixtures ask "does the rule fire?", which is the easy half. This one
asks the question that decides whether anybody keeps the tool installed: does
it stay quiet on code that is *already right* but does not look like the
textbook? Every entry below is a real pattern from real deployments, not a
contrived string.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# Not DJS-001. This reads as a defect at a glance -- DEBUG assigned from the
# environment, no False in sight -- but the comparison resolves to False
# wherever DJANGO_DEBUG is unset, which is everywhere it matters.
DEBUG = os.environ.get("DJANGO_DEBUG") == "1"

# Not DJS-002 or DJS-003. The key is read off disk at startup, so the string in
# the repository is a *path*, not a secret. A rule matching on the setting name
# holding a literal would fire on the file name.
SECRET_KEY = (BASE_DIR / "secrets" / "django.key").read_text().strip()

# Not DJS-005. Named exactly like a credential, holds an import path -- the
# same trap as vulnerable_project's NOTIFICATION_TOKEN, kept here as well
# because the two fixtures fail for different reasons if this regresses.
STRIPE_SECRET_KEY_BACKEND = "app.billing.StripeBackend"
API_TOKEN_HEADER = "X-Api-Token"

# Not DJS-013. The leading dot is Django's own subdomain syntax and matches
# example.test plus everything under it; it is *not* the wildcard, and
# validate_host treats the two completely differently. This is the most common
# correct ALLOWED_HOSTS in production and a sloppy substring check flags it.
ALLOWED_HOSTS = [".example.test", "internal.example.test"]

# Not DJS-014. A scheme-qualified wildcard subdomain, which Django has
# supported since 4.0 and documents. It looks broad and is exactly as broad as
# the deployment is.
CSRF_TRUSTED_ORIGINS = ["https://*.example.test"]

# Not DJS-015 and not DJS-016. Credentials are allowed, which is the
# precondition for DJS-016, but the origin list is explicit -- so the wildcard
# half is absent and the pair must stay silent.
CORS_ALLOWED_ORIGINS = ["https://app.example.test"]
CORS_ALLOW_CREDENTIALS = True

# Not DJS-017. SAMEORIGIN is Django's default and permits framing by the same
# origin, which the admin itself relies on. It is weaker than DENY and it is
# not "clickjacking protection is off".
X_FRAME_OPTIONS = "SAMEORIGIN"

SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True

SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

# Not DJS-011. Explicitly True, which is also the default -- present because
# somebody checked, which is the opposite of the defect.
SESSION_COOKIE_HTTPONLY = True

# Not DJS-011 either. Named like the session cookie flag, is not it: the CSRF
# cookie has to be readable by JavaScript for the standard header-based
# double-submit to work, and Django's own default is False.
CSRF_COOKIE_HTTPONLY = False

# Not DJS-019. MD5 is present, which is the entire signature of the defect, but
# it is *last*. Django hashes with the first entry and only ever reads the rest
# to verify an existing hash, so this is a legacy-verification tail on an
# Argon2 project -- the correct way to migrate, and a rule that greps the list
# for a fast hasher punishes it.
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.MD5PasswordHasher",
]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "corsheaders",
    "rest_framework",
    "django_filters",
    "catalog",
]

# Not DJS-024. The toolbar is installed only when DEBUG is on, and DEBUG above
# is False in every deployment. This is the shape the rule has to tell apart
# from an unconditional entry, and getting it wrong flags most of Django.
if DEBUG:
    INSTALLED_APPS = [*INSTALLED_APPS, "debug_toolbar", "django_extensions"]

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
        "NAME": "app",
        "USER": "app",
        # Not DJS-004. Empty, not missing: the connection authenticates by
        # client certificate, so there is no password to leak and the empty
        # string is deliberate.
        "PASSWORD": "",
        "HOST": "db.example.test",
        "CONN_MAX_AGE": 60,
        "CONN_HEALTH_CHECKS": True,
        # Not DJS-023. ATOMIC_REQUESTS is in the alias, which is the only place
        # Django reads it. The defect is the same name at module level.
        "ATOMIC_REQUESTS": True,
        "OPTIONS": {
            "sslmode": "verify-full",
            "sslrootcert": "/etc/ssl/certs/db-ca.pem",
        },
    }
}

# Not DJS-012. The WSGI spelling, which is what lands in request.META, and the
# proxy is the only thing that can reach this service. DJS-012 has an
# informational branch for it; what it must not do is treat a correct value as
# the defect.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Not DJS-027. include_html is absent, so the mail carries the traceback text
# and nothing else, and the reporter filter *extends* Django's rather than
# replacing it -- the shape of somebody redacting more, not less.
LOGGING = {
    "version": 1,
    "handlers": {
        "mail_admins": {
            "class": "django.utils.log.AdminEmailHandler",
            "level": "ERROR",
        },
    },
    "loggers": {"django.request": {"handlers": ["mail_admins"], "level": "ERROR"}},
}

DEFAULT_EXCEPTION_REPORTER_FILTER = "config.reporting.StricterFilter"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
STATIC_URL = "static/"
USE_TZ = True

# Not DJA-001. The project default is a permission class the project wrote
# itself, so a rule that recognises `IsAuthenticated` by name recognises
# nothing here -- and a project is under no obligation to use DRF's names.
# What the rule has to establish is that the value is *not* AllowAny, which is
# a different question from whether it knows what the value is.
REST_FRAMEWORK = {
    "DEFAULT_PERMISSION_CLASSES": ["catalog.permissions.IsActiveStaffOrReadOnly"],
    "DEFAULT_AUTHENTICATION_CLASSES": ["rest_framework.authentication.SessionAuthentication"],
    # Not DJA-013. No PAGE_SIZE, and none needed: SizedPagination assigns its
    # own page_size, so nothing here is left to the setting.
    "DEFAULT_PAGINATION_CLASS": "catalog.views.SizedPagination",
    "DEFAULT_FILTER_BACKENDS": ["django_filters.rest_framework.DjangoFilterBackend"],
    "DEFAULT_THROTTLE_CLASSES": ["rest_framework.throttling.AnonRateThrottle"],
    "DEFAULT_THROTTLE_RATES": {"anon": "20/hour", "user": "1000/hour"},
}

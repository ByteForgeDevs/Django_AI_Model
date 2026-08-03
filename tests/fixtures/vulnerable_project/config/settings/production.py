"""Production settings.

PLANTED DEFECT: DEBUG left enabled in a production module. Expected at
critical/certain -- the worst finding in this fixture.

PLANTED DEFECT: an HSTS ramp-up that was started and never finished. One hour
is far short of the year the preload list wants (DJS-007), and includeSubDomains
was never switched on (DJS-008). The pair is deliberate: it is the only state in
which DJS-008 has anything to say, since excluding subdomains is the correct
value while HSTS is off.

PLANTED DEFECT: ALLOWED_HOSTS opened to "*" (DJS-013), which switches off Host
header validation -- and Django builds password reset links from that header.

PLANTED DEFECT: SECURE_PROXY_SSL_HEADER spelled as an HTTP header rather than a
WSGI environment key (DJS-012). Django looks this up in request.META, never
finds it, and falls back to the real connection scheme -- so the setting does
nothing at all and says nothing about it.
"""

from .base import *  # noqa: F401,F403

DEBUG = True

ALLOWED_HOSTS = ["*"]

# Copied from a Django 3 answer: without a scheme these match nothing at all.
CSRF_TRUSTED_ORIGINS = ["app.example.test", "https://admin.example.test"]

# The pre-3.5 spelling, which django-cors-headers still honours.
CORS_ORIGIN_ALLOW_ALL = True

# Switched off to make a PDF download render inline. It did not help.
SECURE_CONTENT_TYPE_NOSNIFF = False

SECURE_HSTS_SECONDS = 3600
SECURE_HSTS_INCLUDE_SUBDOMAINS = False

SECURE_PROXY_SSL_HEADER = ("X-Forwarded-Proto", "https")

# PLANTED DEFECT: ATOMIC_REQUESTS at module level (DJS-023), which is not a
# Django setting at all -- it is a key inside a DATABASES alias. The line does
# nothing, nothing warns, and whoever added it now believes a view that raises
# halfway through rolls back.
ATOMIC_REQUESTS = True

# PLANTED DEFECT: the toolbar is one environment variable away from running in
# production, and the package is already on the machine (DJS-025). This is the
# shape the rule exists for -- correct today, and flipped by an operator
# debugging an incident at the worst possible moment.
if os.environ.get("ENABLE_DEBUG_TOOLBAR"):
    INSTALLED_APPS = [*INSTALLED_APPS, "debug_toolbar"]

# PLANTED DEFECT: include_html mails the full HTML debug page -- every local
# variable in every frame -- through SMTP on every unhandled exception, and the
# replacement reporter filter throws away the redaction that would have kept the
# Authorization header and the session cookie out of it (DJS-027).
LOGGING = {
    "version": 1,
    "handlers": {
        "mail_admins": {
            "class": "django.utils.log.AdminEmailHandler",
            "level": "ERROR",
            "include_html": True,
        },
    },
    "loggers": {"django.request": {"handlers": ["mail_admins"], "level": "ERROR"}},
}

DEFAULT_EXCEPTION_REPORTER_FILTER = "app.reporting.TerseFilter"

# PLANTED DEFECT: HttpOnly switched off so an analytics snippet could read the
# session id out of document.cookie (DJS-011). It is the only rule in the family
# that needs an explicit line -- Django's default is True, so this can only ever
# appear because somebody typed it, and the session id becomes readable by every
# script on the page, including any that gets injected onto it.
SESSION_COOKIE_HTTPONLY = False

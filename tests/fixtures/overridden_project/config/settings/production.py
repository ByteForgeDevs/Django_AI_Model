"""Production settings that correctly disable DEBUG.

CONTROL CASE: base.py enables DEBUG, but this module unconditionally turns it
off. That is the normal split-settings pattern, so the base finding must be
downgraded to low/tentative rather than reported as a real problem.

CONTROL CASE: the transport and cookie flags are all set correctly here, so
DJS-006, DJS-007, DJS-008, DJS-009 and DJS-010 must stay silent. DJS-012
stays silent too, by never setting SECURE_PROXY_SSL_HEADER: absent is its safe
value, so there is nothing to write down. Django ships every one of them
off, which means a rule for them fires on a project that simply never mentions
them -- and this fixture is what proves that setting them is enough to stop it.
"""

from .base import *  # noqa: F401,F403

DEBUG = False

ALLOWED_HOSTS = ["app.example.test"]

# Full scheme://host on every entry, and no wildcard to widen who is trusted.
CSRF_TRUSTED_ORIGINS = ["https://app.example.test", "https://admin.example.test"]

# The modern names, with the origins named rather than waived.
CORS_ALLOW_ALL_ORIGINS = False
CORS_ALLOWED_ORIGINS = ["https://app.example.test"]

SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True

# Control for DJS-027: the filter extends Django's rather than replacing it,
# which is the right way to redact more, and the mail_admins handler leaves
# include_html off, so the traceback goes out as plain text.
LOGGING = {
    "version": 1,
    "handlers": {
        "mail_admins": {"class": "django.utils.log.AdminEmailHandler", "level": "ERROR"},
    },
    "loggers": {"django.request": {"handlers": ["mail_admins"], "level": "ERROR"}},
}

DEFAULT_EXCEPTION_REPORTER_FILTER = "app.reporting.ExtraFilter"

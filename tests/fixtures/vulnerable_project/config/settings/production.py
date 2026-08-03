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

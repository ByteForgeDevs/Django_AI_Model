"""Production settings.

PLANTED DEFECT: DEBUG left enabled in a production module. Expected at
critical/certain -- the worst finding in this fixture.

PLANTED DEFECT: an HSTS ramp-up that was started and never finished. One hour
is far short of the year the preload list wants (DJS-007), and includeSubDomains
was never switched on (DJS-008). The pair is deliberate: it is the only state in
which DJS-008 has anything to say, since excluding subdomains is the correct
value while HSTS is off.
"""

from .base import *  # noqa: F401,F403

DEBUG = True

ALLOWED_HOSTS = ["app.example.test"]

SECURE_HSTS_SECONDS = 3600
SECURE_HSTS_INCLUDE_SUBDOMAINS = False

"""Production settings.

PLANTED DEFECT: DEBUG left enabled in a production module. Expected at
critical/certain -- the worst finding in this fixture.
"""

from .base import *  # noqa: F401,F403

DEBUG = True

ALLOWED_HOSTS = ["app.example.test"]

"""Development settings.

CONTROL CASE, NOT A DEFECT: DEBUG = True is correct here. Reporting it would be
a false positive, so this file is listed under `must_not_report` in the manifest.
"""

from .base import *  # noqa: F401,F403

DEBUG = True

ALLOWED_HOSTS = ["localhost", "127.0.0.1"]

"""Production settings that correctly disable DEBUG.

CONTROL CASE: base.py enables DEBUG, but this module unconditionally turns it
off. That is the normal split-settings pattern, so the base finding must be
downgraded to low/tentative rather than reported as a real problem.
"""

from .base import *  # noqa: F401,F403

DEBUG = False

ALLOWED_HOSTS = ["app.example.test"]

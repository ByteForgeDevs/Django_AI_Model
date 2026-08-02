"""Production settings that correctly disable DEBUG.

CONTROL CASE: base.py enables DEBUG, but this module unconditionally turns it
off. That is the normal split-settings pattern, so the base finding must be
downgraded to low/tentative rather than reported as a real problem.

CONTROL CASE: SECURE_SSL_REDIRECT is set correctly here, so
DJS-006 must stay silent. Django ships every one of them
off, which means a rule for it fires on a project that simply never mentions
it -- and this fixture is what proves that setting it is enough to stop it.
"""

from .base import *  # noqa: F401,F403

DEBUG = False

ALLOWED_HOSTS = ["app.example.test"]

SECURE_SSL_REDIRECT = True

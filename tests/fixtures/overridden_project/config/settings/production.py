"""Production settings that correctly disable DEBUG.

CONTROL CASE: base.py enables DEBUG, but this module unconditionally turns it
off. That is the normal split-settings pattern, so the base finding must be
downgraded to low/tentative rather than reported as a real problem.

CONTROL CASE: the transport and cookie flags are all set correctly here, so
DJS-006, DJS-009 and DJS-010 must stay silent. Django ships every one of them
off, which means a rule for them fires on a project that simply never mentions
them -- and this fixture is what proves that setting them is enough to stop it.
"""

from .base import *  # noqa: F401,F403

DEBUG = False

ALLOWED_HOSTS = ["app.example.test"]

SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

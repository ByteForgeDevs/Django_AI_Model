"""The four settings that do nothing without ``SecurityMiddleware``.

``django/middleware/security.py`` loads ``SECURE_SSL_REDIRECT``,
``SECURE_HSTS_SECONDS``, ``SECURE_HSTS_INCLUDE_SUBDOMAINS`` and
``SECURE_CONTENT_TYPE_NOSNIFF`` in its ``__init__`` and nothing else in Django
reads any of them. Four rules checked those values and none checked whether
anything was listening, so a project that wrote every one of them down
correctly and left the middleware out got silence from all four: no redirect,
no HSTS, no nosniff, and a settings file saying otherwise.

That is a worse failure than the one the rules were built for, because the
ordinary failure at least looks like what it is. A reader who greps for
SECURE_SSL_REDIRECT finds `True` and stops.
"""

from pathlib import Path

import pytest

from djaudit import engine
from djaudit.models import Confidence, Severity

MARKERS = "import os\nSECRET_KEY = os.environ['K']\nDEBUG = False\n"

WITH_SECURITY = """MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
]
"""

WITHOUT_SECURITY = """MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
]
"""

CONDITIONAL = """MIDDLEWARE = ["django.middleware.common.CommonMiddleware"]
if os.environ.get("HARDENED"):
    MIDDLEWARE = [*MIDDLEWARE, "django.middleware.security.SecurityMiddleware"]
"""

# Every one of these is the value the documentation asks for.
SAFE = """SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_CONTENT_TYPE_NOSNIFF = True
"""

GUARDED = ("DJS-006", "DJS-007", "DJS-008", "DJS-018")


def build(tmp_path: Path, middleware: str, body: str = SAFE) -> Path:
    root = tmp_path / "project"
    path = root / "myproj/settings.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(MARKERS + middleware + body)
    return root


def audit(root: Path) -> dict[str, str]:
    result = engine.run(root, min_severity=Severity.INFO, min_confidence=Confidence.TENTATIVE)
    assert not result.rule_errors, result.rule_errors
    return {f.rule_id: f.message for f in result.findings if f.rule_id in GUARDED}


class TestTheGap:
    def test_every_setting_correct_and_the_middleware_missing_is_four_findings(self, tmp_path):
        """The case that used to be silent. Nothing here is switched on."""
        assert sorted(audit(build(tmp_path, WITHOUT_SECURITY))) == list(GUARDED)

    @pytest.mark.parametrize("rule_id", GUARDED)
    def test_the_message_names_the_middleware_rather_than_the_value(self, tmp_path, rule_id):
        """The value is right. Telling anyone to change it would waste their time."""
        message = audit(build(tmp_path, WITHOUT_SECURITY))[rule_id]
        assert "MIDDLEWARE does not contain" in message
        assert "django.middleware.security.SecurityMiddleware" in message


class TestWhenItStaysQuiet:
    def test_the_middleware_installed_is_silence(self, tmp_path):
        assert audit(build(tmp_path, WITH_SECURITY)) == {}

    def test_a_middleware_list_we_cannot_fully_read_is_not_an_accusation(self, tmp_path):
        """One branch adds it and one does not, so we did not learn anything.

        Reading "could not tell" as "absent" would fire on every project that
        assembles MIDDLEWARE conditionally, which is a great many of them.
        """
        assert audit(build(tmp_path, CONDITIONAL)) == {}

    def test_a_module_with_no_middleware_at_all_is_not_a_deployment(self, tmp_path):
        """Django's default MIDDLEWARE is empty, so the letter of it agrees.

        A settings module that never mentions middleware is a fragment or a
        test harness rather than something serving traffic, and announcing that
        its security headers are inert would be true, useless and loud.
        """
        assert audit(build(tmp_path, "")) == {}

    def test_a_setting_left_at_its_default_is_not_reported_this_way(self, tmp_path):
        """Nobody expressed an intent, so there is no contradiction to point at.

        DJS-006 and DJS-007 still fire here, for the ordinary reason -- the
        values are off -- and that is the finding. Saying it twice would be one
        mistake and two tickets.
        """
        reported = audit(build(tmp_path, WITHOUT_SECURITY, body=""))
        assert sorted(reported) == ["DJS-006", "DJS-007"]
        assert all("MIDDLEWARE does not contain" not in m for m in reported.values())


class TestSplitSettings:
    def test_an_heir_repeating_the_safe_value_is_not_treated_as_a_correction(self, tmp_path):
        """The base is inert and the heir is inert in exactly the same way.

        The shared policy downgrades a base that every environment overrides,
        because that is ordinary practice. It reasons about the value, and here
        the value was never the problem -- so applying it would drop a certain
        finding to low/tentative and point the reader at the wrong file.
        """
        root = tmp_path / "project"
        settings = root / "myproj/settings"
        settings.mkdir(parents=True)
        (settings / "__init__.py").write_text("")
        (settings / "base.py").write_text(MARKERS + WITHOUT_SECURITY + SAFE)
        (settings / "production.py").write_text(
            "from .base import *  # noqa: F401,F403\nSECURE_SSL_REDIRECT = True\n"
        )
        (root / "manage.py").write_text(
            "import os\nos.environ.setdefault('DJANGO_SETTINGS_MODULE', "
            "'myproj.settings.production')\n"
        )

        result = engine.run(root, min_severity=Severity.INFO, min_confidence=Confidence.TENTATIVE)
        redirect = [f for f in result.findings if f.rule_id == "DJS-006"]
        assert redirect
        assert all(f.severity is Severity.MEDIUM for f in redirect)
        assert all("should override it" not in f.message for f in redirect)

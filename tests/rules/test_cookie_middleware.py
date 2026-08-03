"""The cookie flag rules only speak for Django while Django sets the cookie.

``SESSION_COOKIE_SECURE`` is not read by the session framework at large -- it is
read by ``SessionMiddleware`` at the moment it writes the header. pretix removes
that middleware and installs its own, which passes ``secure=request.is_secure()``
and adds the ``__Host-`` prefix over HTTPS: stricter than the setting, on a
project that never assigns it. Read as ``certain``, DJS-009 and DJS-010 called
those cookies insecure and were simply wrong.

So the finding stays -- a replacement may equally well have dropped the flag --
and stops claiming to be a fact.
"""

from pathlib import Path

from djaudit import engine
from djaudit.models import Confidence, Severity

MARKERS = "INSTALLED_APPS = []\nDEBUG = False\nDATABASES = {}\nSECRET_KEY = 'x'\n"

DJANGO_SESSION = "django.contrib.sessions.middleware.SessionMiddleware"
DJANGO_CSRF = "django.middleware.csrf.CsrfViewMiddleware"


def build(tmp_path: Path, body: str, middleware_source: str | None = None) -> Path:
    root = tmp_path / "project"
    path = root / "myproj/settings.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(MARKERS + body)
    if middleware_source is not None:
        (root / "myproj/mw.py").write_text(middleware_source)
    return root


def audit(root: Path, rule_id: str):
    findings = engine.run(
        root, min_severity=Severity.INFO, min_confidence=Confidence.TENTATIVE
    ).findings
    return [f for f in findings if f.rule_id == rule_id]


def middleware(*entries: str) -> str:
    listed = ", ".join(repr(e) for e in entries)
    return f"MIDDLEWARE = [{listed}]\n"


class TestDjangoStillOwnsTheCookie:
    """``FIRM``, not ``CERTAIN``: ``CERTAIN`` is only the ceiling these rules are
    allowed to reach, and an *absent* setting is read off Django's default rather
    than off an assignment, which grades one step down on its own. What matters
    here is that nothing drags it below that."""

    def test_stock_middleware_keeps_full_confidence(self, tmp_path):
        root = build(tmp_path, middleware(DJANGO_SESSION))
        (finding,) = audit(root, "DJS-009")
        assert finding.confidence is Confidence.FIRM

    def test_an_unassigned_middleware_list_keeps_full_confidence(self, tmp_path):
        """Silence is not evidence that Django was replaced."""
        root = build(tmp_path, "")
        (finding,) = audit(root, "DJS-009")
        assert finding.confidence is Confidence.FIRM

    def test_an_unrelated_middleware_keeps_full_confidence(self, tmp_path):
        root = build(tmp_path, middleware(DJANGO_SESSION, "myproj.mw.AuditMiddleware"))
        (finding,) = audit(root, "DJS-009")
        assert finding.confidence is Confidence.FIRM

    def test_a_list_we_cannot_read_at_all_keeps_full_confidence(self, tmp_path):
        root = build(tmp_path, "import os\nMIDDLEWARE = os.environ['MW'].split(',')\n")
        (finding,) = audit(root, "DJS-009")
        assert finding.confidence is Confidence.FIRM


SETS_IT = """
class SessionMiddleware:
    def process_response(self, request, response):
        response.set_cookie("sid", secure=request.is_secure(), httponly=True)
        return response
"""

IGNORES_IT = """
class SessionMiddleware:
    def process_response(self, request, response):
        response.set_cookie("sid")
        return response
"""


class TestTheReplacementCanBeRead:
    """pretix's own shape: the middleware is project source, so read it."""

    def test_a_replacement_that_sets_the_flag_is_not_reported(self, tmp_path):
        root = build(tmp_path, middleware("myproj.mw.SessionMiddleware"), SETS_IT)
        assert not audit(root, "DJS-009")

    def test_a_replacement_that_ignores_the_flag_keeps_full_confidence(self, tmp_path):
        """Better evidenced than the ordinary case, so it must not be downgraded."""
        root = build(tmp_path, middleware("myproj.mw.SessionMiddleware"), IGNORES_IT)
        (finding,) = audit(root, "DJS-009")
        assert finding.confidence is Confidence.FIRM

    def test_httponly_is_read_off_its_own_keyword(self, tmp_path):
        """The session middleware above sets httponly too, so DJS-011 is silent."""
        root = build(
            tmp_path,
            "SESSION_COOKIE_HTTPONLY = False\n" + middleware("myproj.mw.SessionMiddleware"),
            SETS_IT,
        )
        assert not audit(root, "DJS-011")

    def test_a_replacement_setting_only_secure_leaves_httponly_reported(self, tmp_path):
        root = build(
            tmp_path,
            "SESSION_COOKIE_HTTPONLY = False\n" + middleware("myproj.mw.SessionMiddleware"),
            IGNORES_IT.replace('set_cookie("sid")', 'set_cookie("sid", secure=True)'),
        )
        assert audit(root, "DJS-011")


class TestProjectCodeOwnsTheCookie:
    def test_a_replacement_we_cannot_read_downgrades_and_names_itself(self, tmp_path):
        """No such module in the project, so it came from a dependency."""
        root = build(tmp_path, middleware("thirdparty.mw.SessionMiddleware"))
        (finding,) = audit(root, "DJS-009")
        assert finding.confidence is Confidence.TENTATIVE
        assert "thirdparty.mw.SessionMiddleware" in finding.message + str(finding.rationale)

    def test_the_csrf_cookie_is_read_the_same_way(self, tmp_path):
        root = build(tmp_path, middleware("thirdparty.mw.CsrfViewMiddleware"))
        (finding,) = audit(root, "DJS-010")
        assert finding.confidence is Confidence.TENTATIVE

    def test_httponly_is_read_the_same_way(self, tmp_path):
        root = build(
            tmp_path,
            "SESSION_COOKIE_HTTPONLY = False\n" + middleware("thirdparty.mw.SessionMiddleware"),
        )
        (finding,) = audit(root, "DJS-011")
        assert finding.confidence is Confidence.TENTATIVE

    def test_keeping_django_alongside_a_replacement_keeps_confidence(self, tmp_path):
        """If Django's middleware is still installed, it still sets the cookie."""
        root = build(tmp_path, middleware(DJANGO_SESSION, "thirdparty.mw.SessionMiddleware"))
        (finding,) = audit(root, "DJS-009")
        assert finding.confidence is Confidence.FIRM


class TestConditionalMiddleware:
    """pretix's exact shape: three branches, one of which cannot be resolved."""

    def test_unreadable_branches_do_not_veto_the_readable_ones(self, tmp_path):
        root = build(
            tmp_path,
            "import os\n"
            "MIDDLEWARE = ['thirdparty.mw.SessionMiddleware']\n"
            "if os.environ.get('X'):\n"
            "    MIDDLEWARE = MIDDLEWARE + os.environ['EXTRA'].split(',')\n",
        )
        (finding,) = audit(root, "DJS-009")
        assert finding.confidence is Confidence.TENTATIVE

    def test_django_in_any_readable_branch_keeps_confidence(self, tmp_path):
        root = build(
            tmp_path,
            "import os\n"
            "if os.environ.get('X'):\n"
            f"    MIDDLEWARE = [{DJANGO_SESSION!r}]\n"
            "else:\n"
            "    MIDDLEWARE = ['thirdparty.mw.SessionMiddleware']\n",
        )
        (finding,) = audit(root, "DJS-009")
        assert finding.confidence is Confidence.FIRM

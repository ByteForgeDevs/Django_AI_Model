"""DJS-004 -- a database password readable in the source.

The point of interest is that this has to work when the surrounding setting
does not resolve. Every real DATABASES block takes its host or name from the
environment, which collapses the whole dict to unknown, so a rule that waited
for a resolved value would find nothing anywhere and look like it worked.
"""

from pathlib import Path

from djaudit import engine
from djaudit.models import Confidence, Severity

MARKERS = "INSTALLED_APPS = []\nDEBUG = False\nSECRET_KEY = 'x'\n"
PASSWORD = "not-a-real-password-fixture-only"


def build(tmp_path: Path, body: str) -> Path:
    root = tmp_path / "project"
    path = root / "myproj/settings.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("import os\n" + MARKERS + body)
    return root


def audit(root: Path, rule: str = "DJS-004"):
    findings = engine.run(
        root, min_severity=Severity.INFO, min_confidence=Confidence.TENTATIVE
    ).findings
    return [f for f in findings if f.rule_id == rule]


def databases(**aliases: str) -> str:
    body = ", ".join(f'"{alias}": {{{config}}}' for alias, config in aliases.items())
    return f"DATABASES = {{{body}}}\n"


class TestDetection:
    def test_a_password_in_the_source_is_critical_and_certain(self, tmp_path):
        root = build(tmp_path, databases(default=f'"PASSWORD": "{PASSWORD}"'))
        (finding,) = audit(root)
        assert finding.severity is Severity.CRITICAL
        assert finding.confidence is Confidence.CERTAIN

    def test_it_survives_a_sibling_entry_that_does_not_resolve(self, tmp_path):
        """The case that matters: real config always has env values beside it."""
        root = build(
            tmp_path,
            databases(default=f'"HOST": os.environ["DB_HOST"], "PASSWORD": "{PASSWORD}"'),
        )
        assert len(audit(root)) == 1

    def test_it_points_at_the_password_not_at_the_setting(self, tmp_path):
        root = build(
            tmp_path,
            'DATABASES = {\n    "default": {\n        "NAME": "app",\n'
            f'        "PASSWORD": "{PASSWORD}",\n    }}\n}}\n',
        )
        (finding,) = audit(root)
        assert finding.location.line == 8
        assert "PASSWORD" in finding.location.snippet

    def test_each_alias_is_its_own_finding(self, tmp_path):
        """Separate servers, separate credentials, separate rotations."""
        root = build(
            tmp_path,
            databases(
                default=f'"PASSWORD": "{PASSWORD}"',
                analytics=f'"PASSWORD": "{PASSWORD}-two"',
            ),
        )
        assert len(audit(root)) == 2

    def test_the_alias_is_named_so_the_reader_knows_which_server(self, tmp_path):
        root = build(tmp_path, databases(analytics=f'"PASSWORD": "{PASSWORD}"'))
        (finding,) = audit(root)
        assert "'analytics'" in finding.message

    def test_the_engine_is_reported_because_it_decides_what_is_at_risk(self, tmp_path):
        root = build(
            tmp_path,
            databases(
                default=f'"ENGINE": "django.db.backends.postgresql", "PASSWORD": "{PASSWORD}"'
            ),
        )
        (finding,) = audit(root)
        assert any("django.db.backends.postgresql" in e.content for e in finding.evidence)

    def test_an_environment_fallback_is_reported_but_only_firm(self, tmp_path):
        root = build(
            tmp_path,
            databases(default=f'"PASSWORD": os.environ.get("PW", "{PASSWORD}")'),
        )
        (finding,) = audit(root)
        assert finding.confidence is Confidence.FIRM
        assert "falls back to" in finding.message


class TestControls:
    def test_a_password_read_from_the_environment_is_not_reported(self, tmp_path):
        root = build(tmp_path, databases(default='"PASSWORD": os.environ["DB_PASSWORD"]'))
        assert not audit(root)

    def test_an_alias_with_no_password_is_not_reported(self, tmp_path):
        root = build(tmp_path, databases(default='"NAME": "app", "USER": "app"'))
        assert not audit(root)

    def test_an_empty_password_is_not_reported(self, tmp_path):
        """Django reads that as 'no password', which is a connection problem."""
        root = build(tmp_path, databases(default='"PASSWORD": ""'))
        assert not audit(root)

    def test_a_project_with_no_databases_setting_is_not_reported(self, tmp_path):
        root = build(tmp_path, "")
        assert not audit(root)

    def test_a_databases_setting_built_by_a_call_is_not_reported(self, tmp_path):
        """dj_database_url and friends: nothing readable, so nothing to say."""
        root = build(tmp_path, "DATABASES = {'default': parse(os.environ['URL'])}\n")
        assert not audit(root)

    def test_development_settings_are_left_alone(self, tmp_path):
        root = tmp_path / "project"
        (root / "myproj/settings").mkdir(parents=True)
        (root / "myproj/settings/__init__.py").write_text("")
        (root / "myproj/settings/base.py").write_text(MARKERS)
        (root / "myproj/settings/development.py").write_text(
            "from .base import *\n" + databases(default=f'"PASSWORD": "{PASSWORD}"')
        )
        assert not audit(root)


class TestRedaction:
    def test_the_password_never_appears_in_the_finding(self, tmp_path):
        root = build(tmp_path, databases(default=f'"PASSWORD": "{PASSWORD}"'))
        (finding,) = audit(root)
        assert PASSWORD not in str(finding.to_dict())

    def test_the_snippet_is_redacted_too(self, tmp_path):
        root = build(tmp_path, databases(default=f'"PASSWORD": "{PASSWORD}"'))
        (finding,) = audit(root)
        assert PASSWORD not in finding.location.snippet


class TestSettingBuiltWithoutALiteral:
    """The `getattr(configuration, ...)` and helper-function shapes.

    NetBox writes every setting this way, so a rule that only walked dict
    literals would report nothing on it and look like it had checked. Neither
    benchmark target actually hides a password here -- these are synthetic.
    """

    def test_a_password_inside_a_getattr_fallback_is_found(self, tmp_path):
        root = build(
            tmp_path,
            "import configuration\n"
            "DATABASES = getattr(configuration, 'DATABASES', "
            f'{{"default": {{"PASSWORD": "{PASSWORD}"}}}})\n',
        )
        (finding,) = audit(root)
        assert "'default'" in finding.message

    def test_such_a_finding_is_only_firm(self, tmp_path):
        """A deployment supplying the setting never sees the fallback."""
        root = build(
            tmp_path,
            "import configuration\n"
            "DATABASES = getattr(configuration, 'DATABASES', "
            f'{{"default": {{"PASSWORD": "{PASSWORD}"}}}})\n',
        )
        (finding,) = audit(root)
        assert finding.confidence is Confidence.FIRM

    def test_a_password_returned_by_a_local_helper_is_found(self, tmp_path):
        root = build(
            tmp_path,
            "def db_config():\n"
            f'    return {{"default": {{"PASSWORD": "{PASSWORD}"}}}}\n'
            "DATABASES = db_config()\n",
        )
        assert audit(root)

    def test_it_points_at_the_assignment_when_there_is_no_literal_to_point_at(self, tmp_path):
        root = build(
            tmp_path,
            "def db_config():\n"
            f'    return {{"default": {{"PASSWORD": "{PASSWORD}"}}}}\n'
            "DATABASES = db_config()\n",
        )
        (finding,) = audit(root)
        assert finding.location.snippet.startswith("DATABASES =")

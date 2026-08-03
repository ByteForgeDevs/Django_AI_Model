"""DJS-002 -- SECRET_KEY readable in the source.

The control cases matter more than the positives here. A secret rule that fires
on `os.environ['SECRET_KEY']` is a rule people switch off, and switching it off
also switches off the case it exists to catch.
"""

from pathlib import Path

from djaudit import engine
from djaudit.models import Confidence, Severity

MARKERS = "INSTALLED_APPS = []\nDATABASES = {}\nDEBUG = False\n"
REAL_KEY = "k9x2m4p7q1w8e3r6t5y0u9i8o7p6a5s4d3f2g1h0j9k8l7z6x5c4v3b2n1m0q9w8"


def build(tmp_path: Path, body: str, name: str = "myproj/settings.py") -> Path:
    root = tmp_path / "project"
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return root


def audit(root: Path, rule: str = "DJS-002"):
    findings = engine.run(
        root, min_severity=Severity.INFO, min_confidence=Confidence.TENTATIVE
    ).findings
    return [f for f in findings if f.rule_id == rule]


class TestDetection:
    def test_a_key_written_into_the_source_is_critical_and_certain(self, tmp_path):
        root = build(tmp_path, MARKERS + f'SECRET_KEY = "{REAL_KEY}"\n')
        (finding,) = audit(root)
        assert finding.severity is Severity.CRITICAL
        assert finding.confidence is Confidence.CERTAIN

    def test_it_says_the_key_is_in_the_source(self, tmp_path):
        root = build(tmp_path, MARKERS + f'SECRET_KEY = "{REAL_KEY}"\n')
        (finding,) = audit(root)
        assert "directly in the source" in finding.message

    def test_an_environment_fallback_is_reported_as_a_fallback(self, tmp_path):
        root = build(
            tmp_path,
            "import os\n" + MARKERS + f"SECRET_KEY = os.environ.get('KEY', '{REAL_KEY}')\n",
        )
        (finding,) = audit(root)
        assert "fallback" in finding.message

    def test_an_environment_fallback_is_only_firm(self, tmp_path):
        """The deployment may supply a real key, so we do not claim certainty."""
        root = build(
            tmp_path,
            "import os\n" + MARKERS + f"SECRET_KEY = os.environ.get('KEY', '{REAL_KEY}')\n",
        )
        (finding,) = audit(root)
        assert finding.confidence is Confidence.FIRM

    def test_a_key_inherited_from_a_base_module_is_found(self, tmp_path):
        root = tmp_path / "project"
        (root / "config/settings").mkdir(parents=True)
        (root / "config/settings/__init__.py").write_text("")
        (root / "config/settings/base.py").write_text(MARKERS + f'SECRET_KEY = "{REAL_KEY}"\n')
        (root / "config/settings/production.py").write_text("from .base import *\n")
        modules = {f.properties["settings_module"] for f in audit(root)}
        assert "config.settings.production" in modules


class TestControlCases:
    """Each of these would be a false positive."""

    def test_a_key_read_from_the_environment_is_not_reported(self, tmp_path):
        root = build(tmp_path, "import os\n" + MARKERS + "SECRET_KEY = os.environ['KEY']\n")
        assert audit(root) == []

    def test_a_key_read_from_an_unresolvable_helper_is_not_reported(self, tmp_path):
        root = build(tmp_path, MARKERS + "SECRET_KEY = vault_lookup('key')\n")
        assert audit(root) == []

    def test_an_unset_key_is_not_reported(self, tmp_path):
        """Django refuses to start without one, so it is not our problem."""
        root = build(tmp_path, MARKERS + "ROOT_URLCONF = 'urls'\n")
        assert audit(root) == []

    def test_an_empty_key_is_not_reported(self, tmp_path):
        root = build(tmp_path, MARKERS + 'SECRET_KEY = ""\n')
        assert audit(root) == []

    def test_development_settings_are_ignored(self, tmp_path):
        root = build(
            tmp_path,
            MARKERS + f'SECRET_KEY = "{REAL_KEY}"\n',
            name="config/settings/development.py",
        )
        assert audit(root) == []


class TestDisclosure:
    """A finding travels further than the source, so it must not carry the key."""

    def test_the_key_is_absent_from_the_source_evidence(self, tmp_path):
        root = build(tmp_path, MARKERS + f'SECRET_KEY = "{REAL_KEY}"\n')
        (finding,) = audit(root)
        source = next(e for e in finding.evidence if e.kind.value == "source")
        assert REAL_KEY not in source.content

    def test_the_key_is_absent_from_every_part_of_the_finding(self, tmp_path):
        root = build(tmp_path, MARKERS + f'SECRET_KEY = "{REAL_KEY}"\n')
        (finding,) = audit(root)
        assert REAL_KEY not in str(finding.to_dict())

    def test_the_redaction_records_the_length(self, tmp_path):
        root = build(tmp_path, MARKERS + f'SECRET_KEY = "{REAL_KEY}"\n')
        (finding,) = audit(root)
        source = next(e for e in finding.evidence if e.kind.value == "source")
        assert f"redacted:{len(REAL_KEY)} chars" in source.content

    def test_the_shape_of_the_key_is_still_described(self, tmp_path):
        root = build(tmp_path, MARKERS + f'SECRET_KEY = "{REAL_KEY}"\n')
        (finding,) = audit(root)
        config = " ".join(e.content for e in finding.evidence if e.kind.value == "config")
        assert f"length={len(REAL_KEY)}" in config
        assert "lowercase, digits" in config

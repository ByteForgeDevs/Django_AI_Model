"""DJS-001 -- DEBUG enabled in a production-reachable settings module.

The grading logic is the interesting part. A rule that reports every DEBUG=True
it can find is worse than useless: developers see it fire on their development
settings, conclude the tool does not understand Django, and switch it off.
"""

from pathlib import Path

from djaudit import engine
from djaudit.models import Confidence, Severity

MARKERS = "INSTALLED_APPS = []\nSECRET_KEY = 'x'\nDATABASES = {}\n"


def build(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "project"
    for relative, body in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    return root


def audit(root: Path):
    return engine.run(
        root, min_severity=Severity.INFO, min_confidence=Confidence.TENTATIVE
    ).findings


class TestProductionReachable:
    def test_production_module_is_critical_and_certain(self, tmp_path):
        root = build(
            tmp_path,
            {
                "config/settings/base.py": MARKERS,
                "config/settings/production.py": "from .base import *\nDEBUG = True\n",
            },
        )
        (finding,) = audit(root)
        assert finding.rule_id == "DJS-001"
        assert finding.severity is Severity.CRITICAL
        assert finding.confidence is Confidence.CERTAIN

    def test_lone_settings_module_is_critical_but_only_firm(self, tmp_path):
        """We cannot prove a single settings.py is the one deployed."""
        root = build(tmp_path, {"myproj/settings.py": MARKERS + "DEBUG = True\n"})
        (finding,) = audit(root)
        assert finding.severity is Severity.CRITICAL
        assert finding.confidence is Confidence.FIRM

    def test_unrecognised_module_name_is_still_reported(self, tmp_path):
        root = build(tmp_path, {"config/settings/whatever.py": MARKERS + "DEBUG = True\n"})
        (finding,) = audit(root)
        assert finding.severity is Severity.HIGH
        assert finding.confidence is Confidence.FIRM


class TestControlCases:
    """Each of these would be a false positive. None may ever be reported."""

    def test_development_settings_are_ignored(self, tmp_path):
        root = build(tmp_path, {"config/settings/development.py": MARKERS + "DEBUG = True\n"})
        assert audit(root) == []

    def test_local_settings_are_ignored(self, tmp_path):
        root = build(tmp_path, {"config/settings/local.py": MARKERS + "DEBUG = True\n"})
        assert audit(root) == []

    def test_test_settings_are_ignored(self, tmp_path):
        root = build(tmp_path, {"config/settings/test.py": MARKERS + "DEBUG = True\n"})
        assert audit(root) == []

    def test_debug_false_is_not_reported(self, tmp_path):
        root = build(tmp_path, {"myproj/settings.py": MARKERS + "DEBUG = False\n"})
        assert audit(root) == []

    def test_environment_driven_debug_is_not_reported(self, tmp_path):
        """The recommended pattern. Unresolvable statically, so we stay quiet."""
        root = build(
            tmp_path,
            {
                "myproj/settings.py": "import os\n"
                + MARKERS
                + "DEBUG = os.environ.get('DJANGO_DEBUG', '') == '1'\n"
            },
        )
        assert audit(root) == []

    def test_truthy_non_literal_is_not_reported(self, tmp_path):
        root = build(tmp_path, {"myproj/settings.py": MARKERS + "DEBUG = 1\n"})
        assert audit(root) == []

    def test_a_non_django_settings_file_is_not_audited(self, tmp_path):
        """Some unrelated library's config/settings.py must not be treated as Django."""
        root = build(tmp_path, {"vendor/config/settings.py": "DEBUG = True\n"})
        assert audit(root) == []


class TestGradingModifiers:
    def test_conditional_assignment_drops_confidence(self, tmp_path):
        root = build(
            tmp_path,
            {
                "config/settings/base.py": MARKERS,
                "config/settings/production.py": (
                    "import os\nfrom .base import *\nif os.environ.get('X'):\n    DEBUG = True\n"
                ),
            },
        )
        (finding,) = audit(root)
        assert finding.severity is Severity.CRITICAL
        assert finding.confidence is Confidence.FIRM
        assert "conditional" in finding.message

    def test_base_is_downgraded_when_production_overrides_it(self, tmp_path):
        root = build(
            tmp_path,
            {
                "config/settings/base.py": MARKERS + "DEBUG = True\n",
                "config/settings/production.py": "from .base import *\nDEBUG = False\n",
            },
        )
        (finding,) = audit(root)
        assert finding.severity is Severity.LOW
        assert finding.confidence is Confidence.TENTATIVE
        assert "override" in finding.message

    def test_base_is_not_downgraded_when_the_override_is_conditional(self, tmp_path):
        root = build(
            tmp_path,
            {
                "config/settings/base.py": MARKERS + "DEBUG = True\n",
                "config/settings/production.py": (
                    "import os\nfrom .base import *\nif os.environ.get('X'):\n    DEBUG = False\n"
                ),
            },
        )
        (finding,) = audit(root)
        assert finding.severity is Severity.HIGH


class TestEvidence:
    def test_carries_the_offending_source_line(self, tmp_path):
        root = build(tmp_path, {"myproj/settings.py": MARKERS + "DEBUG = True\n"})
        (finding,) = audit(root)
        source = next(e for e in finding.evidence if e.kind.value == "source")
        assert source.content == "DEBUG = True"

    def test_records_how_the_module_was_classified(self, tmp_path):
        root = build(tmp_path, {"myproj/settings.py": MARKERS + "DEBUG = True\n"})
        (finding,) = audit(root)
        config = next(e for e in finding.evidence if e.kind.value == "config")
        assert "role=primary" in config.content

    def test_exposes_the_role_as_a_machine_readable_property(self, tmp_path):
        root = build(tmp_path, {"myproj/settings.py": MARKERS + "DEBUG = True\n"})
        (finding,) = audit(root)
        assert finding.properties["settings_role"] == "primary"

    def test_points_at_the_right_line(self, tmp_path):
        root = build(tmp_path, {"myproj/settings.py": MARKERS + "DEBUG = True\n"})
        (finding,) = audit(root)
        assert finding.location.line == 4
        assert finding.location.file == "myproj/settings.py"

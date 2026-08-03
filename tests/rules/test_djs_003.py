"""DJS-003 -- SECRET_KEY that can be guessed rather than stolen.

The classifier decides the whole rule, so most of these test it directly: it is
a pure function over a string, and there is no reason to build a Django project
on disk to find out what it thinks of ``"changeme"``. The cases that go through
the engine are the ones about how the rule behaves in a project -- grading, the
handover from DJS-002, and the promise that nothing quotes the key.
"""

from pathlib import Path

import pytest

from djaudit import engine
from djaudit.models import Confidence, Severity
from djaudit.rules.secrets import (
    DJANGO_INSECURE_PREFIX,
    MIN_DISTINCT,
    MIN_KEY_LENGTH,
    normalise,
    weakness,
)

MARKERS = "INSTALLED_APPS = []\nDATABASES = {}\nDEBUG = False\n"

STRONG = "k9x2m4p7q1w8e3r6t5y0u9i8o7p6a5s4d3f2g1h0j9k8l7z6x5"
"""Fifty characters, wide alphabet, no placeholder phrase: DJS-002's case, and
the control that keeps DJS-003 from claiming everything it can read."""


def build(tmp_path: Path, body: str) -> Path:
    root = tmp_path / "project"
    path = root / "myproj/settings.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return root


def audit(root: Path, rule: str = "DJS-003"):
    findings = engine.run(
        root, min_severity=Severity.INFO, min_confidence=Confidence.TENTATIVE
    ).findings
    return [f for f in findings if f.rule_id == rule]


class TestClassifierPositives:
    def test_the_startproject_key_is_recognised_by_its_own_marker(self):
        found = weakness(DJANGO_INSECURE_PREFIX + STRONG)
        assert found is not None
        assert "startproject" in found.kind

    def test_the_marker_wins_over_length_even_when_the_rest_is_strong(self):
        """Django says this key is not for production; that is the better reason."""
        found = weakness(DJANGO_INSECURE_PREFIX + STRONG)
        assert found is not None
        assert "short" not in found.kind

    @pytest.mark.parametrize(
        "secret",
        [
            "changeme",
            "CHANGE_ME",
            "change-me-please",
            "your-secret-key-here",
            "please-replace-me-with-a-real-key-before-deploying",
            "supersecret",
            "s3cr3t",
            "notasecret",
            "my-django-secret-key-goes-here-somewhere-eventually",
            "---",
            "...",
            "secret",
            "TODO",
            "xxx",
        ],
    )
    def test_placeholders_are_caught(self, secret):
        assert weakness(secret) is not None

    def test_a_placeholder_is_named_as_a_placeholder_not_as_a_short_key(self):
        found = weakness("changeme")
        assert found is not None
        assert "placeholder" in found.kind

    def test_a_long_placeholder_phrase_is_caught_inside_a_longer_string(self):
        secret = "abcdefgh-changeme-jklmnopqrstuvwxyz0123456789"
        assert len(secret) >= MIN_KEY_LENGTH
        found = weakness(secret)
        assert found is not None
        assert "placeholder" in found.kind

    def test_a_key_below_the_length_floor_is_caught(self):
        secret = "a1b2c3d4e5f6g7h8"
        assert len(secret) < MIN_KEY_LENGTH
        found = weakness(secret)
        assert found is not None
        assert "short" in found.kind

    def test_the_reason_gives_the_length_so_a_reader_can_judge_it(self):
        found = weakness("a1b2c3d4e5f6g7h8")
        assert found is not None
        assert "16 characters" in found.detail

    def test_a_long_key_with_almost_no_alphabet_is_caught(self):
        secret = "abab" * 20
        assert len(secret) >= MIN_KEY_LENGTH
        assert len(set(secret)) < MIN_DISTINCT
        found = weakness(secret)
        assert found is not None
        assert "patterned" in found.kind

    def test_length_alone_would_have_passed_the_patterned_key(self):
        """Which is the reason the distinct-character test exists at all."""
        secret = "abab" * 20
        assert len(secret) >= MIN_KEY_LENGTH


class TestClassifierControls:
    def test_a_real_generated_key_is_left_alone(self):
        assert weakness(STRONG) is None

    def test_a_django_generated_key_without_the_marker_is_left_alone(self):
        secret = "8v!x@2q#w$e%r^t&y*u(i)o-p_a=b+c1d2e3f4g5h6i7j8k9l0"
        assert weakness(secret) is None

    def test_a_hex_key_at_exactly_the_floor_is_left_alone(self):
        """token_hex(16) is 128 bits and 32 characters -- fine, and on the line."""
        secret = "0123456789abcdef0123456789abcdef"
        assert len(secret) == MIN_KEY_LENGTH
        assert weakness(secret) is None

    @pytest.mark.parametrize(
        "secret",
        [
            "devqzmr7x4kp2wn9bt6yhs3fjcl8gvda05e1u",
            "testify-not-a-word-here-just-fifty-ish-chars-abcd",
            "abcxyz-random-material-following-immediately-1234",
        ],
    )
    def test_short_words_do_not_match_inside_a_longer_key(self, secret):
        """`dev`, `test` and `abc` turn up by chance; only the whole value counts."""
        assert len(secret) >= MIN_KEY_LENGTH
        assert weakness(secret) is None


class TestNormalise:
    def test_case_and_punctuation_are_ignored(self):
        assert normalise("Change_Me!") == normalise("change-me") == "changeme"

    def test_a_value_with_no_alphanumerics_reduces_to_nothing(self):
        assert normalise("---") == ""


class TestInProject:
    def test_a_guessable_key_is_critical_and_certain(self, tmp_path):
        root = build(tmp_path, MARKERS + 'SECRET_KEY = "changeme"\n')
        (finding,) = audit(root)
        assert finding.severity is Severity.CRITICAL
        assert finding.confidence is Confidence.CERTAIN

    def test_djs_002_stays_quiet_so_one_line_is_one_finding(self, tmp_path):
        root = build(tmp_path, MARKERS + 'SECRET_KEY = "changeme"\n')
        assert audit(root, "DJS-003")
        assert not audit(root, "DJS-002")

    def test_djs_002_still_owns_a_strong_committed_key(self, tmp_path):
        root = build(tmp_path, MARKERS + f'SECRET_KEY = "{STRONG}"\n')
        assert audit(root, "DJS-002")
        assert not audit(root, "DJS-003")

    def test_an_environment_fallback_is_only_firm(self, tmp_path):
        """A deployment may supply a real key, so we do not claim certainty."""
        root = build(
            tmp_path,
            "import os\n" + MARKERS + "SECRET_KEY = os.environ.get('KEY', 'changeme')\n",
        )
        (finding,) = audit(root)
        assert finding.confidence is Confidence.FIRM

    def test_an_environment_fallback_is_described_as_falling_back(self, tmp_path):
        root = build(
            tmp_path,
            "import os\n" + MARKERS + "SECRET_KEY = os.environ.get('KEY', 'changeme')\n",
        )
        (finding,) = audit(root)
        assert "falls back to" in finding.message

    def test_a_key_read_from_the_environment_is_not_reported(self, tmp_path):
        root = build(tmp_path, "import os\n" + MARKERS + "SECRET_KEY = os.environ['KEY']\n")
        assert not audit(root)

    def test_development_settings_are_left_alone(self, tmp_path):
        root = tmp_path / "project"
        (root / "myproj/settings").mkdir(parents=True)
        (root / "myproj/settings/__init__.py").write_text("")
        (root / "myproj/settings/base.py").write_text(MARKERS)
        (root / "myproj/settings/development.py").write_text(
            "from .base import *\nSECRET_KEY = 'changeme'\n"
        )
        assert not audit(root)


class TestRedaction:
    def test_the_key_never_appears_anywhere_in_the_finding(self, tmp_path):
        secret = "please-replace-me-with-a-real-key-before-deploying"
        root = build(tmp_path, MARKERS + f'SECRET_KEY = "{secret}"\n')
        (finding,) = audit(root)
        assert secret not in str(finding.to_dict())

    def test_the_snippet_is_redacted_too(self, tmp_path):
        """Location carries its own copy of the line, which is easy to forget."""
        secret = "please-replace-me-with-a-real-key-before-deploying"
        root = build(tmp_path, MARKERS + f'SECRET_KEY = "{secret}"\n')
        (finding,) = audit(root)
        assert secret not in finding.location.snippet
        assert "redacted" in finding.location.snippet

    def test_a_tiny_placeholder_is_shown_rather_than_masked(self, tmp_path):
        """`---` has nothing to protect, and hiding it hides how bad it is."""
        root = build(tmp_path, MARKERS + 'SECRET_KEY = "---"\n')
        (finding,) = audit(root)
        assert "---" in finding.location.snippet

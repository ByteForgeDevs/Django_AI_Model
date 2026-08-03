"""DJS-013 -- ALLOWED_HOSTS is a wildcard or empty.

Two different failures share one rule because they share one setting. The
wildcard is an attack surface; the empty list is a site that answers nothing.
They are graded differently and only one of them is conditional on DEBUG.
"""

from pathlib import Path

import pytest

from djaudit import engine
from djaudit.models import Confidence, Severity
from djaudit.rules._base import any_entry, definitely_empty, entries_of
from djaudit.values import Value

MARKERS = "INSTALLED_APPS = []\nDATABASES = {}\nSECRET_KEY = 'x'\n"


def build(tmp_path: Path, body: str, debug: str = "DEBUG = False\n") -> Path:
    root = tmp_path / "project"
    path = root / "myproj/settings.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("import os\n" + MARKERS + debug + body)
    return root


def audit(root: Path):
    findings = engine.run(
        root, min_severity=Severity.INFO, min_confidence=Confidence.TENTATIVE
    ).findings
    return [f for f in findings if f.rule_id == "DJS-013"]


class TestReadingTheList:
    def test_a_plain_list_is_one_branch(self):
        assert entries_of(Value.of(["a", "b"])) == (["a", "b"],)

    def test_something_unreadable_is_reported_as_unknown_not_as_empty(self):
        """The distinction that keeps both real targets quiet."""
        assert entries_of(Value.unknown("not modelled")) == (None,)
        assert not definitely_empty(Value.unknown("not modelled"))

    def test_a_conditional_contributes_every_branch(self):
        both = Value.conditional([Value.of(["a"]), Value.of(["*"])])
        assert any_entry(both, lambda entry: entry == "*")

    def test_an_unreadable_branch_does_not_make_the_whole_thing_empty(self):
        both = Value.conditional([Value.of([]), Value.unknown("not modelled")])
        assert not definitely_empty(both)


class TestWildcard:
    def test_a_wildcard_is_reported_at_high(self, tmp_path):
        (finding,) = audit(build(tmp_path, 'ALLOWED_HOSTS = ["*"]\n'))
        assert finding.severity is Severity.HIGH
        assert 'contains "*"' in finding.message

    def test_the_message_names_the_consequence_people_underestimate(self, tmp_path):
        (finding,) = audit(build(tmp_path, 'ALLOWED_HOSTS = ["*"]\n'))
        assert "password reset" in finding.message

    def test_a_wildcard_is_reported_even_with_debug_on(self, tmp_path):
        """Unlike the empty case, this one is not excused by DEBUG."""
        root = build(tmp_path, 'ALLOWED_HOSTS = ["*"]\n', debug="DEBUG = True\n")
        assert audit(root)

    def test_a_wildcard_hidden_in_one_branch_is_still_found(self, tmp_path):
        """A ternary collapses to the environment's default, but a branching
        module does not, and the open branch is the one that matters."""
        root = build(
            tmp_path,
            'if os.environ.get("OPEN"):\n'
            '    ALLOWED_HOSTS = ["*"]\n'
            "else:\n"
            '    ALLOWED_HOSTS = ["app.example.test"]\n',
        )
        assert audit(root)

    @pytest.mark.parametrize("hosts", ['[".example.test"]', '["app.example.test"]'])
    def test_real_hostnames_are_not_reported(self, tmp_path, hosts):
        """A leading dot is Django's subdomain wildcard and is entirely normal."""
        assert not audit(build(tmp_path, f"ALLOWED_HOSTS = {hosts}\n"))

    def test_a_star_inside_a_hostname_is_not_the_wildcard(self, tmp_path):
        """Only a bare "*" disables the check; "*.x" is matched literally."""
        assert not audit(build(tmp_path, 'ALLOWED_HOSTS = ["*.example.test"]\n'))


class TestEmpty:
    def test_empty_with_debug_off_is_reported_at_low(self, tmp_path):
        (finding,) = audit(build(tmp_path, "ALLOWED_HOSTS = []\n"))
        assert finding.severity is Severity.LOW
        assert "serves nothing rather than one that is exposed" in finding.message

    def test_never_set_with_debug_off_is_reported(self, tmp_path):
        (finding,) = audit(build(tmp_path, ""))
        assert "is never set, so it is empty" in finding.message

    def test_empty_with_debug_on_is_not_reported(self, tmp_path):
        """Django allows localhost while DEBUG is on, so this is correct."""
        assert not audit(build(tmp_path, "ALLOWED_HOSTS = []\n", debug="DEBUG = True\n"))

    def test_a_list_we_cannot_read_is_not_treated_as_empty(self, tmp_path):
        """Both benchmark targets are this shape, and both must stay quiet."""
        root = build(tmp_path, 'ALLOWED_HOSTS = os.environ["HOSTS"].split(",")\n')
        assert not audit(root)


class TestRemediation:
    def test_it_explains_djangos_subdomain_syntax(self, tmp_path):
        """The near-miss that silently matches nothing is worth naming."""
        (finding,) = audit(build(tmp_path, 'ALLOWED_HOSTS = ["*"]\n'))
        assert '".example.com"' in finding.remediation
        assert "will simply never match" in finding.remediation

    def test_it_says_a_proxy_is_not_a_substitute(self, tmp_path):
        (finding,) = audit(build(tmp_path, 'ALLOWED_HOSTS = ["*"]\n'))
        assert "not a substitute" in finding.remediation

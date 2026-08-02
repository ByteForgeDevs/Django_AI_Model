"""Fingerprints must survive code motion, or committed baselines become useless."""

from djaudit import fingerprint as fp
from djaudit.models import Location
from tests.test_models import make_finding


class TestStability:
    def test_moving_a_line_does_not_change_the_fingerprint(self):
        before = make_finding(location=Location(file="s.py", line=10, snippet="DEBUG = True"))
        after = make_finding(location=Location(file="s.py", line=93, snippet="DEBUG = True"))
        assert fp.assign([before])[0].fingerprint == fp.assign([after])[0].fingerprint

    def test_indentation_and_trailing_whitespace_are_ignored(self):
        plain = make_finding(location=Location(file="s.py", line=1, snippet="DEBUG = True"))
        padded = make_finding(
            location=Location(file="s.py", line=1, snippet="    DEBUG = True   \n")
        )
        assert fp.assign([plain])[0].fingerprint == fp.assign([padded])[0].fingerprint

    def test_normalisation_collapses_internal_whitespace(self):
        assert fp.normalise_snippet("DEBUG   =    True\n") == "DEBUG = True"

    def test_moving_a_file_does_change_the_fingerprint(self):
        here = make_finding(location=Location(file="a/s.py", line=1, snippet="DEBUG = True"))
        there = make_finding(location=Location(file="b/s.py", line=1, snippet="DEBUG = True"))
        assert fp.assign([here])[0].fingerprint != fp.assign([there])[0].fingerprint

    def test_different_rules_on_identical_code_differ(self):
        one = make_finding(rule_id="DJS-001", location=Location("s.py", 1, snippet="x"))
        two = make_finding(rule_id="DJS-002", location=Location("s.py", 1, snippet="x"))
        assigned = fp.assign([one, two])
        assert assigned[0].fingerprint != assigned[1].fingerprint


class TestOccurrences:
    def test_identical_findings_in_one_file_get_distinct_fingerprints(self):
        findings = [
            make_finding(location=Location(file="s.py", line=n, snippet="DEBUG = True"))
            for n in (5, 15, 25)
        ]
        assigned = fp.assign(findings)
        assert len({f.fingerprint for f in assigned}) == 3

    def test_assignment_is_independent_of_input_order(self):
        findings = [
            make_finding(location=Location(file="s.py", line=n, snippet="DEBUG = True"))
            for n in (5, 15, 25)
        ]
        forwards = [f.fingerprint for f in fp.assign(findings)]
        backwards = [f.fingerprint for f in fp.assign(list(reversed(findings)))]
        assert forwards == backwards


class TestVersioning:
    def test_the_scheme_version_is_part_of_the_hash(self):
        digest = fp.compute("DJS-001", "s.py", "DEBUG = True", 0)
        assert digest != fp.compute("DJS-001", "s.py", "DEBUG = True", 1)
        assert len(digest) == 16

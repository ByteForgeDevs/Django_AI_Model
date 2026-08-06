"""Where a finding lives, and what that says about how much it matters.

The same defect is not worth the same everywhere. A query inside a loop is a
query inside a loop whether it sits in a view or in a test, but the view serves
every request and the test runs over five fixture rows on a developer's laptop.
A data migration is worse still: it executes once, against a known row count, and
is then frozen -- Django will not let you edit it after it has been applied
anywhere, so "fix" means writing a second migration to undo the first.

Three rules had run into this independently (DJP-001, DJP-002 and DJP-004, where
35 of 83 findings were in test or migration files) and each was about to settle
it in its own way. Settling it per rule would have meant three different
answers, so it is settled once, here, for every rule in every family.

Two options were rejected:

* **Suppressing them.** Silence is the one behaviour a static analyser cannot
  walk back, because nobody audits what they were not shown. A slow test suite
  is a real complaint, and a data migration that issues one query per row is how
  a deploy times out. These findings are true, and one of them being worth
  acting on is enough reason to print it.
* **Leaving severity alone.** With everything equal, the ranked output is led by
  whatever family happens to be largest, and on NetBox that is test helpers. The
  first screen is the only screen most people read, and spending it on test code
  is how a tool gets a reputation for noise.

So they are reported and demoted one rank. The finding still carries its scope
in ``properties["scope"]``, which is what makes the demotion auditable rather
than a number that quietly differs from the rule's declared severity, and lets a
caller who disagrees filter or re-rank on it.

Classification is by whole path segment. Substring matching would catch
``latest/`` and ``contest.py``; ``PurePosixPath.parts`` cannot.
"""

from __future__ import annotations

from dataclasses import replace
from enum import StrEnum
from pathlib import PurePosixPath

from djaudit.models import Finding, Severity

_DEMOTED: dict[Severity, Severity] = {
    Severity.CRITICAL: Severity.HIGH,
    Severity.HIGH: Severity.MEDIUM,
    Severity.MEDIUM: Severity.LOW,
    Severity.LOW: Severity.INFO,
    Severity.INFO: Severity.INFO,
}

_TEST_DIRS = frozenset({"tests", "testing", "test"})
_TEST_FILES = frozenset({"tests.py", "testing.py", "conftest.py"})
_MIGRATION_DIR = "migrations"


class Scope(StrEnum):
    """Which part of a project a file belongs to."""

    PRODUCTION = "production"
    TESTS = "tests"
    MIGRATIONS = "migrations"

    @property
    def demoted(self) -> bool:
        """Whether findings here are reported one rank below the rule's severity."""
        return self is not Scope.PRODUCTION


def classify(file: str) -> Scope:
    """Classify a project-relative path.

    Migrations win over tests, because a migration inside a test app is still
    frozen once applied, which is the property that makes it hard to fix.
    """
    path = PurePosixPath(file)
    parts = path.parts
    if _MIGRATION_DIR in parts:
        return Scope.MIGRATIONS
    if _TEST_DIRS & set(parts[:-1]):
        return Scope.TESTS
    if path.name.startswith("test_") or path.name in _TEST_FILES:
        return Scope.TESTS
    return Scope.PRODUCTION


def apply(finding: Finding) -> Finding:
    """Record the finding's scope and demote it if it is not production code.

    Severity is not part of the fingerprint, so this never invalidates a
    committed baseline or a triage entry.
    """
    scope = classify(finding.location.file)
    properties = {**finding.properties, "scope": scope.value}
    if not scope.demoted:
        return replace(finding, properties=properties)
    return replace(finding, properties=properties, severity=_DEMOTED[finding.severity])

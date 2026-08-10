"""What an external tool is allowed to add to a djaudit report.

The obvious design for this layer is a translator: run the tool, parse its
output, rename the fields, append. Measured against the benchmark corpus, that
design is a disaster, and the numbers are not close.

Running `ruff --select DJ,S` over the three benchmark projects:

    healthchecks    958 findings   (djaudit reports 41)
    netbox          210 findings   (djaudit reports 76)
    pretix       12,220 findings   (djaudit reports 151)

Twelve thousand two hundred and twenty. A tool that answers a request to audit
pretix with 12,220 results has not helped anyone; it has produced the outcome
the adoptability principle exists to prevent, and it would do it while wearing
our name. So the question this module answers is not "how do we convert a ruff
diagnostic into a `Finding`" -- that part is trivial -- but "which of them has
any business being in the report at all".

Three measurements decide the shape.

**Most of it is test code.** 850 of healthchecks' 958 and 11,857 of pretix's
12,220 sit under `tests/`. `S101` (bare `assert`) is what pytest is made of.
Excluding test files removes 89% of the volume before any judgement is
required, and removes nothing a deployment can be harmed by.

**Some of it contradicts a decision we already made, in writing.** `ruff`'s
`DJ001` reports a nullable string field. So does our `DJD-002` -- except that
`DJD-002` exempts `blank=True`, because the project has then said out loud that
empty is permitted input, and exempts columns spanned by a uniqueness rule,
because Django documents `null` as the way to allow more than one row with no
value. Measured, 151 of the 179 `DJ001` hits outside test code carry
`blank=True`. Importing `DJ001` would not add coverage; it would silently
overrule a documented precision decision 151 times and call the result ours.

**And where we both fire, we say it better.** `DJD-002` reports once per model
and names every affected column, because pretix's `Invoice` declares fifteen of
them and they are one migration to fix, not fifteen. `DJ001` reports fifteen
times. The eight `DJD-002` findings on pretix cover all 28 columns that `DJ001`
finds outside tests.

So every code an external tool can emit must be *claimed* before anything it
says reaches a report, and there are only three honest claims: we have no rule
here and the finding is worth having (`ADOPT`), we own this ground and our
answer is the better one (`SUBSUMED`), or it was measured to be noise
(`REJECTED`). A code nobody has claimed is the interesting case, and it is
neither dropped nor adopted -- see `ClaimTable.unclaimed`.

The reasoning is the same one `provenance.py` makes about labels: silence must
never be readable as a judgement. A tool that ships a new check in a point
release would otherwise have its output either vanish or appear in our report,
and in both cases nobody decided anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Protocol, runtime_checkable

from djaudit.models import (
    Confidence,
    Evidence,
    EvidenceKind,
    Family,
    Finding,
    Location,
    Severity,
    Tier,
)

TEST_DIRECTORIES = frozenset({"tests", "test", "testing"})
"""Directory names whose contents are excluded from every adapter.

Measured on the corpus: 850 of healthchecks' 958 ruff findings and 11,857 of
pretix's 12,220 live under one of these. A bare `assert` is what a test is
made of, and a hardcoded password in a fixture is a fixture.
"""


def is_test_path(relative: str) -> bool:
    """Whether a project-relative path is test code.

    Deliberately syntactic and deliberately broad. The alternative -- asking
    which files pytest would collect -- needs the target's configuration and
    is therefore a live-tier question, and being wrong here costs a missed lint
    in a file whose whole purpose is to fail on purpose.
    """
    parts = PurePosixPath(relative).parts
    if any(part in TEST_DIRECTORIES for part in parts):
        return True
    name = parts[-1] if parts else ""
    return name == "conftest.py" or name.startswith("test_") or name.endswith("_test.py")


@dataclass(frozen=True, slots=True)
class Availability:
    """Whether a tool can be run, and if not, why not.

    A value rather than an exception, and the reasoning is `NullProvider`'s:
    djaudit works without any of these tools, and the way to keep that the
    tested path rather than the theoretical one is to make "absent" an
    ordinary answer that every caller has to handle.
    """

    tool: str
    version: str = ""
    reason: str = ""
    """Empty when the tool is available. Non-empty is the whole failure story."""

    def __bool__(self) -> bool:
        return not self.reason

    def describe(self) -> str:
        if self.reason:
            return f"{self.tool} unavailable: {self.reason}"
        return f"{self.tool} {self.version}" if self.version else self.tool


class Claim(StrEnum):
    """What we have decided about one external rule code.

    Three, and no default. Every code an adapter can produce carries one of
    these because somebody looked, which is the only thing separating this
    layer from an import.
    """

    ADOPT = "adopt"
    """No djaudit rule covers this and the finding is worth having."""

    SUBSUMED = "subsumed"
    """A djaudit rule owns this ground. Ours is reported; this is dropped."""

    REJECTED = "rejected"
    """Measured to be noise here. Dropped, and `why` records the measurement."""


@dataclass(frozen=True, slots=True)
class Claimed:
    """One external rule code and what we decided to do about it.

    The invariants are enforced rather than documented. A `SUBSUMED` entry that
    names no djaudit rule is not a decision, it is a deletion with a comment; an
    `ADOPT` entry with no severity cannot be ranked against anything else in the
    report; and a `REJECTED` entry that names one of our rules is really a
    `SUBSUMED` entry whose author changed their mind halfway through the line.
    """

    code: str
    claim: Claim
    why: str
    ours: tuple[str, ...] = ()
    """djaudit rule ids that cover this ground. Required when `SUBSUMED`."""

    title: str = ""
    family: Family | None = None
    severity: Severity | None = None
    confidence: Confidence = Confidence.FIRM
    remediation: str = ""

    def __post_init__(self) -> None:
        if not self.code:
            raise ValueError("a claim needs a code")
        if not self.why:
            raise ValueError(f"{self.code}: a claim without a reason is not a decision")
        if self.claim is Claim.SUBSUMED and not self.ours:
            raise ValueError(f"{self.code}: subsumed must name the djaudit rule that covers it")
        if self.claim is Claim.REJECTED and self.ours:
            raise ValueError(
                f"{self.code}: rejected names {', '.join(self.ours)}; a code covered "
                f"by one of our rules is subsumed, not rejected"
            )
        if self.claim is Claim.ADOPT:
            missing = [
                name
                for name, value in (
                    ("a title", self.title),
                    ("a family", self.family),
                    ("a severity", self.severity),
                )
                if not value
            ]
            if missing:
                raise ValueError(f"{self.code}: an adopted code needs {', '.join(missing)}")

    @property
    def adopted(self) -> bool:
        return self.claim is Claim.ADOPT


@dataclass(frozen=True, slots=True)
class External:
    """One diagnostic from an external tool, before we have judged it.

    Tool-neutral on purpose: every adapter's job is to get its tool's output
    into this shape, and every decision after that is made once, here, rather
    than four times in four parsers.
    """

    code: str
    message: str
    file: str
    """Project-relative and POSIX-style, like `Location.file`."""

    line: int
    column: int = 1
    end_line: int | None = None
    end_column: int | None = None
    url: str = ""


@dataclass(frozen=True, slots=True)
class ClaimTable:
    """Every code one tool can emit, and what we decided about each.

    `unclaimed` is the part that matters. A tool that adds a check in a point
    release produces a code nobody has ruled on, and both silent outcomes are
    wrong: dropping it means our coverage quietly depends on a version pin, and
    adopting it means shipping a finding nobody has read. So an unclaimed code
    is neither -- it is surfaced, and a gate turns it into a task for a human.
    """

    tool: str
    claims: tuple[Claimed, ...]

    def __post_init__(self) -> None:
        seen = [c.code for c in self.claims]
        duplicated = sorted({code for code in seen if seen.count(code) > 1})
        if duplicated:
            raise ValueError(f"{self.tool}: {', '.join(duplicated)} claimed more than once")

    @property
    def by_code(self) -> dict[str, Claimed]:
        return {c.code: c for c in self.claims}

    def unclaimed(self, found: tuple[External, ...]) -> tuple[str, ...]:
        """Codes present in the output that this table does not rule on.

        Test files are excluded here too. A new check that only ever fires in
        `tests/` is not a decision anybody needs to make.
        """
        known = self.by_code
        return tuple(
            sorted(
                {
                    item.code
                    for item in found
                    if item.code not in known and not is_test_path(item.file)
                }
            )
        )

    def adopt(self, found: tuple[External, ...], availability: Availability) -> tuple[Finding, ...]:
        """The subset worth reporting, mapped into our schema.

        Test files are dropped before the table is consulted, so no claim ever
        has to reason about them and the measurement that justifies the
        exclusion lives in exactly one place.
        """
        known = self.by_code
        kept: list[Finding] = []
        for item in found:
            if is_test_path(item.file):
                continue
            claimed = known.get(item.code)
            if claimed is None or not claimed.adopted:
                continue
            kept.append(as_finding(item, claimed, availability))
        return tuple(kept)


def as_finding(item: External, claimed: Claimed, availability: Availability) -> Finding:
    """Map one adopted diagnostic into a `Finding`.

    The rule id is namespaced with the tool's name because a reader scanning a
    report is entitled to know that `RUFF-S324` was not decided by anything in
    `src/djaudit/rules/`, and because a fingerprint colliding with one of ours
    would make a baseline silently accept a different finding than the one it
    was written for.

    The family is whatever the claim says. There is deliberately no
    `Family.EXT`: grouping a hardcoded password with the other settings
    findings is what makes the report readable, and the id already carries the
    provenance.
    """
    if claimed.family is None or claimed.severity is None:
        raise ValueError(f"{claimed.code}: an adopted code needs a family and a severity")
    return Finding(
        rule_id=f"{availability.tool.upper()}-{item.code}",
        title=claimed.title,
        family=claimed.family,
        severity=claimed.severity,
        confidence=claimed.confidence,
        tier=Tier.STATIC,
        location=Location(
            file=item.file,
            line=item.line,
            column=item.column,
            end_line=item.end_line,
            end_column=item.end_column,
        ),
        message=item.message,
        rationale=claimed.why,
        remediation=claimed.remediation,
        evidence=(
            Evidence(
                kind=EvidenceKind.COMMAND_OUTPUT,
                content=f"{item.code}: {item.message}",
                source=availability.describe(),
            ),
        ),
        references=(item.url,) if item.url else (),
        properties={"external": availability.tool, "code": item.code},
    )


@dataclass(frozen=True, slots=True)
class Report:
    """One adapter's contribution to a run, including what it could not do."""

    tool: str
    findings: tuple[Finding, ...] = ()
    availability: Availability | None = None
    unclaimed: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()

    @property
    def ran(self) -> bool:
        return bool(self.availability)

    def explain(self) -> str:
        """One line for the degradation notice, in `Skipped.explain`'s spirit."""
        if self.availability is None:
            return f"{self.tool} did not run"
        if not self.availability:
            return self.availability.describe()
        return f"{self.availability.describe()} contributed {len(self.findings)} findings"


@runtime_checkable
class Adapter(Protocol):
    """A third-party tool djaudit can run and take some findings from.

    `collect` never raises for anything the tool does. An adapter whose tool is
    missing, whose tool crashes, or whose tool emits output we cannot parse
    returns a `Report` saying so, because the alternative is a security tool
    that abandons a whole audit because an optional linter was not installed.
    """

    @property
    def name(self) -> str: ...

    @property
    def table(self) -> ClaimTable: ...

    def probe(self) -> Availability: ...

    def collect(self, root: object) -> Report: ...

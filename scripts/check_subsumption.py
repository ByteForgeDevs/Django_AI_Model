"""Check that a `SUBSUMED` claim is true where the tool actually reported it.

A claim table is how djaudit avoids reporting the same problem twice: a rule
code an external tool emits and one of our rules covers is marked `SUBSUMED`,
and every one of that tool's findings is then dropped before it becomes a
finding. Deciding overlap once per code, with a written reason, is much better
than guessing at it per location -- see `djaudit.adapters.merge` for the
measurement that settled that.

It has one blind spot, and it is a bad one. The decision is made *per code and
globally*, so it drops the tool's finding at every location, including the
locations where our rule does not in fact fire. The evidence disappears in
silence: the external finding is gone and ours was never made, so nothing in
the report, the benchmark or the triage file records that anything was lost.
Precision gates cannot see it either, because a finding that is never made
cannot be a false positive.

So the claim is checked against the tool that makes it. For every subsumed
code, the tool is run restricted to that code, and every location it reports is
either matched to a finding from the djaudit rule named in the claim, or listed.
The list is recorded per target and this gate fails when it changes.

The list is *not* expected to be empty, and a gate demanding that it be empty
would be wrong. Our rules are deliberately narrower than the linters they
subsume -- `DJD-002` exempts columns carrying `blank=True` and columns spanned
by a uniqueness rule, and says so in its `limitations` -- and being narrower on
purpose is the reason our version is worth having. What the recorded list gives
is that every one of those exclusions has been looked at once, and that a new
one cannot appear without somebody being made to look at it again.

The rule is matched on its evidence as well as its location, because a rule may
report per model where the linter reports per column: `DJD-002` names the first
offending column and lists the rest in its evidence, and treating those as
unmatched would manufacture a shortfall that is not real.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from djaudit import engine
from djaudit.adapters.base import Claim, ClaimTable
from djaudit.adapters.ruff import CLAIMS as RUFF_CLAIMS
from djaudit.adapters.ruff import TOOL as RUFF_TOOL
from djaudit.models import Finding

TABLES: tuple[ClaimTable, ...] = (RUFF_CLAIMS,)
"""Every adapter whose claims this gate knows how to check.

`pip-audit` is absent because it subsumes nothing: it reports published
advisories against pinned versions, which no djaudit rule covers or could.
"""

EVIDENCE_LINE = re.compile(r"# line (\d+)\s*$")
"""How a rule that reports per model points at the columns it grouped.

Matched rather than assumed: a rule that stops writing these would show up here
as a sudden shortfall, which is the correct outcome -- the gate would be unable
to prove the claim any more.
"""


@dataclass(frozen=True, slots=True)
class Reported:
    """One location an external tool named, relative to the project root."""

    code: str
    file: str
    line: int

    def __str__(self) -> str:
        return f"{self.file}:{self.line}"


def run_ruff(root: Path, codes: list[str], executable: str) -> list[Reported]:
    """Every location ruff reports for exactly these codes.

    The flags are the adapter's, minus the code selection: `--isolated` so the
    target's own `pyproject.toml` cannot switch a rule off, `--no-cache` so a
    stale cache cannot answer. A gate invoking the tool differently from the
    adapter would be recording a shortfall in a measurement nobody makes.

    Written to a file rather than read from the pipe: the ruff adapter shipped
    once reading stdout and pretix's report exceeded the 1 MiB a captured stream
    is allowed, arriving truncated mid-object and parsing as nothing at all.
    """
    with tempfile.TemporaryDirectory(prefix="djaudit-subsumption-") as scratch:
        destination = Path(scratch) / "report.json"
        subprocess.run(
            [
                executable,
                "check",
                "--select",
                ",".join(sorted(codes)),
                "--output-format",
                "json",
                "--output-file",
                str(destination),
                "--no-cache",
                "--isolated",
                ".",
            ],
            cwd=root,
            capture_output=True,
            check=False,
            timeout=300,
        )
        if not destination.exists():
            raise SystemExit("ruff wrote no report; the gate cannot prove anything")
        raw = json.loads(destination.read_text(encoding="utf-8"))
    found: list[Reported] = []
    for item in raw:
        try:
            relative = Path(item["filename"]).resolve().relative_to(root.resolve())
        except ValueError:
            continue
        found.append(
            Reported(
                code=str(item["code"]), file=relative.as_posix(), line=int(item["location"]["row"])
            )
        )
    return found


def locations_of(findings: list[Finding], rule_ids: tuple[str, ...]) -> set[tuple[str, int]]:
    """Every line these rules point at, including the ones grouped into evidence."""
    covered: set[tuple[str, int]] = set()
    for finding in findings:
        if finding.rule_id not in rule_ids:
            continue
        covered.add((finding.location.file, finding.location.line))
        for evidence in finding.evidence:
            for line in evidence.content.splitlines():
                match = EVIDENCE_LINE.search(line)
                if match:
                    covered.add((finding.location.file, int(match.group(1))))
    return covered


@dataclass(frozen=True, slots=True)
class Residual:
    """What one subsumed code reports, and how much of it our rule reports back."""

    code: str
    ours: tuple[str, ...]
    reported: int
    unmatched: tuple[str, ...]

    @property
    def matched(self) -> int:
        return self.reported - len(self.unmatched)

    def as_json(self) -> dict[str, object]:
        return {
            "ours": list(self.ours),
            "reported": self.reported,
            "matched": self.matched,
            "unmatched": list(self.unmatched),
        }

    @classmethod
    def from_json(cls, code: str, entry: dict[str, object]) -> Residual:
        return cls(
            code=code,
            ours=tuple(str(o) for o in _sequence(entry.get("ours"))),
            reported=int(str(entry.get("reported", -1))),
            unmatched=tuple(str(u) for u in _sequence(entry.get("unmatched"))),
        )


def _sequence(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else []


def measure(root: Path, executable: str) -> dict[str, Residual]:
    """What each subsumed code reports, and how much of it we report back."""
    subsumed = [c for table in TABLES for c in table.claims if c.claim is Claim.SUBSUMED]
    if not subsumed:
        raise SystemExit(
            "no subsumed claims found; either the tables were emptied or this "
            "gate is reading the wrong ones, and both are failures"
        )
    reported = run_ruff(root, [c.code for c in subsumed], executable)
    ours = engine.run(root).findings

    result: dict[str, Residual] = {}
    for claim in subsumed:
        theirs = [r for r in reported if r.code == claim.code]
        covered = locations_of(ours, claim.ours)
        result[claim.code] = Residual(
            code=claim.code,
            ours=claim.ours,
            reported=len(theirs),
            unmatched=tuple(sorted(str(r) for r in theirs if (r.file, r.line) not in covered)),
        )
    return result


def compare(measured: dict[str, Residual], expected: dict[str, Residual]) -> list[str]:
    """Every way the measurement differs from what was recorded."""
    problems: list[str] = []
    for code in sorted(set(measured) | set(expected)):
        if code not in measured:
            problems.append(f"{code}: recorded but no longer a subsumed claim")
            continue
        if code not in expected:
            problems.append(f"{code}: subsumed but never recorded; run with --write and review")
            continue
        was, now = expected[code], measured[code]
        covers = ", ".join(now.ours)
        if was.ours != now.ours:
            problems.append(f"{code}: claims {covers}, recorded as {', '.join(was.ours)}")
        if was.reported != now.reported:
            problems.append(
                f"{code}: the tool reports {now.reported} locations, recorded {was.reported}"
            )
        for where in sorted(set(now.unmatched) - set(was.unmatched)):
            problems.append(
                f"{code}: {where} is dropped as subsumed by {covers} but that "
                f"rule does not report it, and this is new"
            )
        for where in sorted(set(was.unmatched) - set(now.unmatched)):
            problems.append(f"{code}: {where} was unmatched and no longer is; re-record it")
    return problems


def read(path: Path) -> dict[str, Residual]:
    """The recorded expectation, or nothing if it was never written."""
    document = json.loads(path.read_text(encoding="utf-8"))
    claims = document.get("claims")
    if not isinstance(claims, dict):
        raise SystemExit(f"{path}: no claims object, so there is nothing to check against")
    return {str(code): Residual.from_json(str(code), entry) for code, entry in claims.items()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--name", required=True)
    parser.add_argument("--expect", type=Path)
    parser.add_argument("--write", action="store_true", help="record the measurement instead")
    parser.add_argument("--executable", default=RUFF_TOOL)
    args = parser.parse_args()

    measured = measure(args.root, args.executable)
    destination = args.expect or Path("benchmarks/subsumption") / f"{args.name}.json"

    if args.write:
        destination.parent.mkdir(parents=True, exist_ok=True)
        document = {
            "target": args.name,
            "claims": {code: r.as_json() for code, r in sorted(measured.items())},
        }
        destination.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        print(f"recorded {destination}")
        return 0

    if not destination.exists():
        print(f"no recorded subsumption for {args.name}: {destination}", file=sys.stderr)
        return 1

    problems = compare(measured, read(destination))
    for residual in sorted(measured.values(), key=lambda r: r.code):
        print(
            f"  {residual.code} -> {', '.join(residual.ours)}: {residual.reported} reported, "
            f"{residual.matched} matched, {len(residual.unmatched)} excluded by design"
        )
    if problems:
        print(f"\nsubsumption changed on {args.name}:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    print(f"subsumption holds on {args.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

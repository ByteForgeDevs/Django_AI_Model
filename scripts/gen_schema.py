"""Record the published contract, and refuse to let it change quietly.

Three things we ship are contracts with people we cannot contact:

* the JSON report shape, which any consumer parses;
* the enum vocabularies inside it, since a new ``severity`` value can walk
  straight past a consumer's ``match`` and be dropped;
* the fingerprint algorithm, which is the identity of a finding across runs
  and therefore the meaning of every baseline file committed downstream.

The third is the one with no visible symptom. Changing how a snippet is
normalised alters no field name and breaks no test that checks fingerprints
are *stable* -- they stay perfectly stable, at new values, and every baseline
in every downstream repository silently stops matching. The next run reports
findings that were accepted months ago as brand new.

So this file keeps a ledger rather than a snapshot. Each ``SCHEMA_VERSION``
gets one entry describing the shape published under it, and each
``FINGERPRINT_VERSION`` gets a set of vectors. Entries are append-only: the
gate re-derives the current shape from the code and demands it match the
entry for the current version exactly. A change therefore has only two ways
out -- revert it, or publish it as a new version with a new entry -- and
rewriting an old entry to match new code is caught by comparing against what
git already has.

Usage::

    python scripts/gen_schema.py            # write the ledger
    python scripts/gen_schema.py --check    # verify it, for CI
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from enum import StrEnum
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from djaudit import __version__  # noqa: E402
from djaudit import fingerprint as fp  # noqa: E402
from djaudit.context import Diagnostic, ProjectContext, SettingsModule, SettingsRole  # noqa: E402
from djaudit.degradation import Degradation, Skipped  # noqa: E402
from djaudit.engine import RunResult  # noqa: E402
from djaudit.models import (  # noqa: E402
    SCHEMA_VERSION,
    Confidence,
    Evidence,
    EvidenceKind,
    Family,
    Finding,
    Location,
    Severity,
    Tier,
)
from djaudit.provenance import DETERMINISTIC, Verdict  # noqa: E402
from djaudit.reporters import json_reporter  # noqa: E402

LEDGER = ROOT / "schema" / "contract.json"
DOC = ROOT / "docs" / "versioning.md"

ENUMS: tuple[type[StrEnum], ...] = (Severity, Confidence, Tier, Family, EvidenceKind)

# Paths whose keys are values, not field names. ``summary.by_severity`` is
# deliberately absent: its keys are the Severity enum, so they are contract.
OPEN_MAPS = frozenset({"parse_errors", "rule_errors", "findings[].properties"})

# Inputs chosen to exercise the parts of the algorithm that could drift without
# any test noticing: whitespace collapsing, the occurrence index, an empty
# snippet, and non-ASCII text that a careless encoding change would rewrite.
VECTORS: tuple[tuple[str, str, str, int], ...] = (
    ("DJS-001", "settings.py", "DEBUG = True", 0),
    ("DJS-001", "settings.py", "DEBUG = True", 1),
    ("DJS-001", "settings.py", "DEBUG   =\n\tTrue", 0),
    ("DJP-004", "app/views.py", "for x in qs:", 0),
    ("DJI-002", "app/db.py", "", 0),
    ("DJX-009", "app/m.py", "verbose_name = 'caf\u00e9 \u2014 na\u00efve'", 0),
)


def _typename(value: object) -> str:
    """Describe a value's type the way a consumer would experience it."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if value is None:
        return "null"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def shape(payload: object, prefix: str = "") -> dict[str, str]:
    """Flatten a payload into ``key path -> type`` pairs.

    Lists collapse to a single ``[]`` step, so a report with three findings and
    a report with one describe the same contract. Open maps collapse the same
    way: their keys are a filename, a rule id, whatever a project happened to
    contain, and recording those would put one specimen's data in the contract
    and demand a version bump the day the specimen changed.
    """
    out: dict[str, str] = {}
    if isinstance(payload, dict):
        if prefix in OPEN_MAPS:
            out[prefix] = "map"
            return out
        for key, value in payload.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            out[path] = _typename(value)
            out.update(shape(value, path))
    elif isinstance(payload, list):
        for item in payload:
            out[f"{prefix}[]"] = _typename(item)
            out.update(shape(item, f"{prefix}[]"))
    return out


def _specimen() -> RunResult:
    """A run that populates every optional branch of the report.

    Derived from a real report rather than hand-written, because a hand-written
    list of field names is a second copy of the schema and drifts from it.
    Anything the reporter can emit must appear here, or the contract records
    only the fields that happen to be busy.
    """
    location = Location(
        file="app/views.py",
        line=12,
        column=5,
        end_line=14,
        end_column=9,
        snippet="qs = Thing.objects.all()",
    )
    finding = Finding(
        rule_id="DJP-001",
        title="Query in a loop",
        family=Family.DJP,
        severity=Severity.HIGH,
        confidence=Confidence.FIRM,
        tier=Tier.STATIC,
        location=location,
        message="message",
        rationale="rationale",
        remediation="remediation",
        evidence=(Evidence(kind=EvidenceKind.AST, content="content", source="source"),),
        references=("https://example.invalid/",),
        fingerprint="0123456789abcdef",
        properties={"key": "value"},
    )
    context = ProjectContext(
        root=ROOT,
        python_files=(ROOT / "x.py",),
        django_version="5.2",
        settings_entrypoint="proj.settings",
        settings_modules=(
            SettingsModule(
                path=ROOT / "proj" / "settings.py",
                dotted="proj.settings",
                role=SettingsRole.PRIMARY,
                is_entrypoint=True,
            ),
        ),
        diagnostics=(Diagnostic(code="D001", message="message", detail="detail", blocking=False),),
        parse_errors={ROOT / "broken.py": "invalid syntax"},
    )
    return RunResult(
        context=context,
        findings=[finding],
        total_raw=1,
        rules_run=1,
        rule_errors={"DJS-001": "boom"},
        degraded=Degradation(
            reason="the live tier was not requested",
            skipped=(
                Skipped(
                    rule_id="DJM-010",
                    title="title",
                    fallback="fallback",
                    covered_by=("DJM-003",),
                ),
            ),
        ),
    )


def _merge(shapes: list[dict[str, str]]) -> dict[str, str]:
    """Union the types seen at each path across specimens.

    A field that is an integer when set and null when not is both, and saying
    so is the point: a consumer reading the contract needs to know which
    fields it must handle as absent.
    """
    merged: dict[str, set[str]] = {}
    for one in shapes:
        for path, kind in one.items():
            merged.setdefault(path, set()).add(kind)
    return {path: "|".join(sorted(kinds)) for path, kinds in sorted(merged.items())}


def _bare() -> RunResult:
    """A run with every optional left at its default.

    The other half of the contract: this is what the report looks like on a
    clean project, and the shape it produces has to be describable too.
    """
    finding = Finding(
        rule_id="DJS-001",
        title="title",
        family=Family.DJS,
        severity=Severity.LOW,
        confidence=Confidence.TENTATIVE,
        tier=Tier.STATIC,
        location=Location(file="settings.py", line=1),
        message="message",
    )
    return RunResult(context=ProjectContext(root=ROOT), findings=[finding])


def contract() -> dict[str, Any]:
    """The contract as the code currently defines it.

    Derived from a fully-populated run *and* a bare one, because optionality is
    part of the contract and a single specimen can only ever show one side of
    it. Recording ``end_line`` as an integer because the specimen set it would
    tell every consumer a field is always present that is usually null.
    """
    verdicts = {"0123456789abcdef": Verdict(label="true_positive", provenance=DETERMINISTIC)}
    return {
        "shape": _merge(
            [
                shape(json_reporter.build(_specimen(), verdicts)),
                shape(json_reporter.build(_bare(), None)),
            ]
        ),
        "enums": {e.__name__: [m.value for m in e] for e in ENUMS},
    }


def vectors() -> dict[str, str]:
    """Pinned digests, keyed by their inputs.

    The key carries all four inputs and is asserted unique: two vectors
    colliding on a key would leave one silently overwritten, shrinking the
    pinned set with nothing to show for it.
    """
    out = {repr((r, f, s, o)): fp.compute(r, f, s, o) for r, f, s, o in VECTORS}
    if len(out) != len(VECTORS):
        raise AssertionError(f"vector keys collide: {len(out)} keys for {len(VECTORS)} vectors")
    return out


def _release_series(version: str) -> str:
    """The part of a version that a breaking change must move.

    Below 1.0 SemVer gives no compatibility promise on the major, so the minor
    carries it; at and above 1.0 the major does.
    """
    parts = version.split(".")
    return parts[0] if int(parts[0]) > 0 else ".".join(parts[:2])


def build() -> dict[str, Any]:
    """The ledger, adding entries but never touching published ones.

    ``setdefault`` rather than assignment, and the difference is not cosmetic.
    Assigning would refresh ``tool_version`` on an already-published entry every
    time the release number moved, so a routine patch release would rewrite
    history and trip the append-only check on its way out. It would also let a
    developer launder a schema change by running the generator, which is the
    one escape route this whole file exists to close.
    """
    existing = json.loads(LEDGER.read_text()) if LEDGER.exists() else {}
    schemas = dict(existing.get("schemas", {}))
    prints = dict(existing.get("fingerprints", {}))
    schemas.setdefault(str(SCHEMA_VERSION), {"tool_version": __version__, **contract()})
    prints.setdefault(fp.FINGERPRINT_VERSION, {"tool_version": __version__, "vectors": vectors()})
    return {"schemas": schemas, "fingerprints": prints}


def _committed() -> dict[str, Any] | None:
    """The ledger as git has it, or None when it is not committed yet."""
    rel = LEDGER.relative_to(ROOT).as_posix()
    try:
        blob = subprocess.run(
            ["git", "show", f"HEAD:{rel}"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if blob.returncode != 0:
        return None
    try:
        parsed: dict[str, Any] = json.loads(blob.stdout)
    except json.JSONDecodeError:
        return None
    return parsed


def _describe(current: dict[str, str], recorded: dict[str, str]) -> list[str]:
    problems = []
    for key in sorted(set(recorded) - set(current)):
        problems.append(f"  removed: {key} (was {recorded[key]})")
    for key in sorted(set(current) - set(recorded)):
        problems.append(f"  added:   {key} ({current[key]})")
    for key in sorted(set(current) & set(recorded)):
        if current[key] != recorded[key]:
            problems.append(f"  retyped: {key} ({recorded[key]} -> {current[key]})")
    return problems


def _check_schema(ledger: dict[str, Any], problems: list[str]) -> None:
    entry = ledger.get("schemas", {}).get(str(SCHEMA_VERSION))
    if entry is None:
        problems.append(
            f"SCHEMA_VERSION is {SCHEMA_VERSION} and the ledger has no entry for it; "
            f"run scripts/gen_schema.py"
        )
        return
    live = contract()
    drift = _describe(live["shape"], entry.get("shape", {}))
    if drift:
        problems.append(
            f"the report shape no longer matches schema {SCHEMA_VERSION}:\n"
            + "\n".join(drift)
            + f"\n  a published shape is frozen: bump SCHEMA_VERSION to {SCHEMA_VERSION + 1} "
            f"and record the new shape, or revert"
        )
    for name, members in live["enums"].items():
        was = entry.get("enums", {}).get(name)
        if was is None:
            problems.append(f"enum {name} is not recorded under schema {SCHEMA_VERSION}")
        elif was != members:
            problems.append(
                f"enum {name} changed under schema {SCHEMA_VERSION}: "
                f"{was} -> {members}; a consumer matching on these values will not "
                f"recognise the new one, so this needs SCHEMA_VERSION {SCHEMA_VERSION + 1}"
            )


def _check_fingerprints(ledger: dict[str, Any], problems: list[str]) -> None:
    entry = ledger.get("fingerprints", {}).get(fp.FINGERPRINT_VERSION)
    if entry is None:
        problems.append(
            f"FINGERPRINT_VERSION is {fp.FINGERPRINT_VERSION!r} and the ledger has no "
            f"vectors for it; run scripts/gen_schema.py"
        )
        return
    live = vectors()
    recorded = entry.get("vectors", {})
    changed = [k for k in sorted(set(live) & set(recorded)) if live[k] != recorded[k]]
    if changed:
        problems.append(
            f"the fingerprint algorithm changed under {fp.FINGERPRINT_VERSION!r} "
            f"({len(changed)} of {len(recorded)} vectors differ, first: {changed[0]!r}); "
            f"every baseline committed downstream stops matching and its findings "
            f"reappear as new. Change FINGERPRINT_VERSION, or revert"
        )
    for missing in sorted(set(recorded) - set(live)):
        problems.append(f"vector {missing!r} was dropped; published vectors are frozen")


def _check_history(ledger: dict[str, Any], problems: list[str]) -> None:
    """Published entries are append-only, so compare against what git holds."""
    was = _committed()
    if was is None:
        return
    for section in ("schemas", "fingerprints"):
        for key, entry in was.get(section, {}).items():
            now = ledger.get(section, {}).get(key)
            if now is None:
                problems.append(f"{section} entry {key!r} was deleted from the ledger")
            elif now != entry:
                problems.append(
                    f"{section} entry {key!r} was edited in place; it describes what has "
                    f"already been published, so it can only be added to, never rewritten"
                )
    live = _release_series(__version__)
    for section, current in (
        ("schemas", str(SCHEMA_VERSION)),
        ("fingerprints", fp.FINGERPRINT_VERSION),
    ):
        if current in was.get(section, {}):
            continue
        prior = [e.get("tool_version", "0.0.0") for e in was.get(section, {}).values()]
        # `any`, not `all`: the evidence that this version did not get its own
        # release is that *some* already-published entry shares the current one.
        # Requiring every prior entry to match made the check fire exactly once,
        # at the first bump, and go quiet forever after -- from the second entry
        # onwards there is always an older series to make `all` false, so a third
        # schema version could ride along on a release that had already shipped a
        # second.
        if any(_release_series(p) == live for p in prior):
            problems.append(
                f"{section} gained entry {current!r} without a release bump: still "
                f"{__version__}. A new {section[:-1]} version is a breaking change for "
                f"consumers, so it needs its own release"
            )


# Where each published number lives. The doc states this in a table, and a
# table is exactly the kind of thing that keeps its shape while going stale.
CONSTANTS = (
    ("__version__", "src/djaudit/__init__.py"),
    ("SCHEMA_VERSION", "src/djaudit/models.py"),
    ("FINGERPRINT_VERSION", "src/djaudit/fingerprint.py"),
)


def _check_doc(problems: list[str]) -> None:
    """The policy is only a policy while it still describes the code."""
    if not DOC.exists():
        problems.append(f"{DOC.relative_to(ROOT)} is missing")
        return
    text = DOC.read_text()
    # Deliberately not anchored to backticks. A path written with arguments,
    # like `scripts/gen_schema.py --check`, is still a claim that the file
    # exists, and requiring the whole span to be a path skipped exactly those.
    cited = re.findall(r"(?:src|docs|scripts|tests|schema)/[\w./-]+", text)
    for path in sorted({c.rstrip(".,;:") for c in cited}):
        if not (ROOT / path).exists():
            problems.append(f"versioning.md cites {path}, which does not exist")
    for name, home in CONSTANTS:
        if name not in text:
            problems.append(f"versioning.md never mentions {name}")
        elif f"`{home}`" not in text:
            problems.append(f"versioning.md mentions {name} but not its home, {home}")
        if not re.search(rf"^{name}\b", (ROOT / home).read_text(), re.MULTILINE):
            problems.append(f"versioning.md says {name} lives in {home}; it does not")
    for path in sorted(OPEN_MAPS):
        if f"`{path}`" not in text:
            problems.append(
                f"{path} collapses to an open map but versioning.md does not say so; "
                f"a reader would expect its keys to be contract"
            )
    for enum in ENUMS:
        if f"`{enum.__name__}`" not in text:
            problems.append(
                f"versioning.md never names the {enum.__name__} enum it freezes; "
                f"a reader cannot tell which vocabularies a bump protects"
            )


def check() -> int:
    if not LEDGER.exists():
        print(f"::error::{LEDGER.relative_to(ROOT)} is missing; run scripts/gen_schema.py")
        return 1
    ledger = json.loads(LEDGER.read_text())
    problems: list[str] = []
    _check_schema(ledger, problems)
    _check_fingerprints(ledger, problems)
    _check_history(ledger, problems)
    _check_doc(problems)
    if ledger != build():
        problems.append("the ledger is out of date; run scripts/gen_schema.py")
    if problems:
        for problem in problems:
            print(f"::error::{problem}")
        return 1
    schemas = len(ledger["schemas"])
    print(
        f"contract current: schema {SCHEMA_VERSION} of {schemas} · "
        f"{len(ledger['schemas'][str(SCHEMA_VERSION)]['shape'])} fields · "
        f"{len(ENUMS)} enums · {len(vectors())} fingerprint vectors "
        f"under {fp.FINGERPRINT_VERSION}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify instead of writing")
    args = parser.parse_args(argv)
    if args.check:
        return check()
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(build(), indent=2, sort_keys=True) + "\n")
    print(f"wrote {LEDGER.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""pip-audit: the one external tool that says something djaudit cannot.

The bandit measurement in 5.3.3 declined an adapter because ruff already said
everything bandit did. This is the opposite case and the reason the adapter
layer was worth building at all. Nothing in djaudit's 87 rules reads a version
number and asks whether it is known-vulnerable, because answering that needs a
vulnerability database and a database is not something a static analyser can
carry. On netbox's pinned `requirements.txt` the tool is silent; on five
deliberately old pins it returns 114 advisory entries. It is additive by
construction.

Two decisions in here are load-bearing and neither is obvious.

**`--disable-pip`.** pip-audit's default is to build a virtual environment and
run pip's resolver over the requirements file, which is how it reaches
*transitive* dependencies. That is also how it downloads packages from an index
and, for any sdist, executes that package's build backend to obtain metadata.
The static tier promises it never executes the target -- "safe on untrusted
code" is the whole reason the tier exists -- and a requirements file is
untrusted input like any other. Resolution is therefore off, and the cost is
recorded rather than hidden: netbox pins 46 packages directly, and the one real
vulnerability in the corpus (`pyjwt 2.12.1`, five advisories) arrives through
`social-auth-core` and is invisible to us. Auditing what is *installed* rather
than what is *declared* belongs to the live tier, which already accepts that it
runs the target's code.

Measured, the flag is also what makes the tool usable: netbox takes 19s frozen
against 102s resolving, healthchecks 7s against 21s, and a requirements file
with conflicting pins fails outright under resolution while auditing fine
frozen.

**We choose which requirements to send.** `--disable-pip` refuses a file
containing any unpinned requirement, and it refuses the whole file rather than
the line -- pretix pins 12 of its 76 production dependencies exactly, so one
`babel` aborts the other 75. Sending only the exactly-pinned lines, from a file
we write ourselves, turns "pretix cannot be audited" into "12 of 76 audited and
here are the 64 we could not". A wildcard like `bleach==6.4.*` is unpinned for
this purpose: the installed version is whatever resolved on deploy day, and
auditing a guess would produce a clean report about a version nobody runs.

`--vulnerability-service osv` because the default PyPI service timed out at 82s
on a 15-package file while OSV answered the same file in 21s.

Development manifests are excluded. `manifest.py` already draws that line
conservatively, and a vulnerability in a linter that never ships is not a
finding about the deployment.
"""

from __future__ import annotations

import json
import re
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from djaudit import manifest
from djaudit.adapters.base import Availability, Claim, Claimed, ClaimTable, Report
from djaudit.adapters.process import probe_tool, run_tool
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

TOOL = "pip-audit"

ARGUMENTS = (
    "--format",
    "json",
    "--disable-pip",
    "--no-deps",
    "--vulnerability-service",
    "osv",
    "--progress-spinner",
    "off",
)
"""`--no-deps` is not the flag it sounds like.

Read from pip-audit's source rather than its help text: `no_deps` only *permits*
`disable_pip` on a file whose requirements are not hash-pinned. `--disable-pip`
is what actually stops the resolver. Passing only `--no-deps` still builds a
virtualenv and runs `pip install --dry-run`, which is how the first measurement
of this tool came back 5x slower than it needed to be and pulled a transitive
`webencodings` into a run that had asked for no dependencies.
"""

EXACTLY_PINNED = re.compile(
    r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*==\s*([^\s,;*]+)\s*$"
)
"""`name==version`, with an optional extras group and no wildcard.

Deliberately stricter than a requirement parser. Anything this does not match
is reported as unaudited rather than guessed at, because a clean report about a
version the deployment does not run is worse than no report.
"""

CLAIMS = ClaimTable(
    tool=TOOL,
    claims=(
        Claimed(
            code="PYSEC",
            claim=Claim.ADOPT,
            why=(
                "The Python Packaging Advisory Database, and the only source "
                "that appeared in any measurement: all 114 advisory entries "
                "from the probe pins and all 5 from netbox's transitive pyjwt "
                "carried this prefix. Each entry names the versions that fix "
                "it, which is what makes the finding actionable rather than "
                "alarming."
            ),
            title="Pinned dependency has a known vulnerability",
            family=Family.DJS,
            severity=Severity.HIGH,
            confidence=Confidence.FIRM,
            remediation=(
                "Upgrade to one of the fixed versions named in the finding, or "
                "record why this deployment is not affected. Where the pin is "
                "transitive, the direct dependency that pulls it in has to move "
                "first."
            ),
        ),
    ),
)
"""Advisory *sources*, not advisory ids.

An id is minted daily and no table could rule on one. A source is a small,
stable set and the decision is real -- databases differ in curation, and one
that started returning unreviewed reports would change what our findings are
worth. Anything else that turns up is surfaced through `unclaimed` instead of
being adopted unread or dropped unnoticed.
"""


@dataclass(frozen=True, slots=True)
class Advisory:
    """One vulnerability, tied back to the line that pinned the version."""

    identifier: str
    package: str
    version: str
    description: str
    fix_versions: tuple[str, ...]
    aliases: tuple[str, ...]
    file: str
    line: int
    raw: str

    @property
    def source(self) -> str:
        """The advisory database, which is what `CLAIMS` rules on."""
        return self.identifier.split("-")[0].upper()


def pinned(requirements: tuple[manifest.Requirement, ...]) -> tuple[list[str], list[str]]:
    """Split declared requirements into what can be audited and what cannot."""
    auditable: list[str] = []
    skipped: list[str] = []
    for requirement in requirements:
        if EXACTLY_PINNED.match(requirement.raw):
            auditable.append(requirement.raw.strip())
        else:
            skipped.append(requirement.raw.strip())
    return auditable, skipped


def by_file(manifests: Iterable[manifest.Manifest]) -> dict[Path, tuple[manifest.Requirement, ...]]:
    """Every declared requirement, grouped by the file that declares it.

    A `pyproject.toml` yields one `Manifest` per optional-dependency group --
    netbox has ten -- and treating each as its own file meant ten subprocess
    invocations and ten near-identical diagnostics about a single unpinned
    line. The file is the unit a reader cares about, and it is also the unit
    that keeps a `Requirement`'s line number meaningful.
    """
    grouped: dict[Path, dict[tuple[str, int], manifest.Requirement]] = {}
    for source in manifests:
        into = grouped.setdefault(source.path, {})
        for requirement in source.requirements:
            # A dependency declared in two optional groups is one line in one
            # file, and counting it twice would inflate every number a reader
            # is given about that file.
            into.setdefault((requirement.raw, requirement.line), requirement)
    return {path: tuple(found.values()) for path, found in grouped.items()}


def parse(
    payload: str, requirements: tuple[manifest.Requirement, ...], relative: str
) -> tuple[Advisory, ...]:
    """Advisories from one pip-audit run, back-referenced to the manifest.

    pip-audit reports a package, not a line, so the line comes from our own
    reading of the same file -- which is also a cross-check: the tool and
    `manifest.py` agreed on the count for every file measured.

    Duplicates are dropped. OSV returns the same advisory more than once for a
    single package (52 of the probe's 114 entries were repeats), and a report
    that lists one CVE five times is a report nobody finishes reading.
    """
    document = json.loads(payload)
    lines = {manifest.normalise(r.name): r for r in requirements}
    found: list[Advisory] = []
    seen: set[tuple[str, str]] = set()
    for dependency in document.get("dependencies", ()):
        name = str(dependency.get("name", ""))
        requirement = lines.get(manifest.normalise(name))
        for vulnerability in dependency.get("vulns", ()):
            identifier = str(vulnerability.get("id", ""))
            if not identifier or (name, identifier) in seen:
                continue
            seen.add((name, identifier))
            found.append(
                Advisory(
                    identifier=identifier,
                    package=name,
                    version=str(dependency.get("version", "")),
                    description=str(vulnerability.get("description", "")).strip(),
                    fix_versions=tuple(str(v) for v in vulnerability.get("fix_versions", ())),
                    aliases=tuple(str(a) for a in vulnerability.get("aliases", ())),
                    file=relative,
                    line=requirement.line if requirement else 1,
                    raw=requirement.raw.strip()
                    if requirement
                    else f"{name}=={dependency.get('version', '')}",
                )
            )
    return tuple(found)


def summarise(description: str) -> str:
    """The first sentence of an advisory, for a one-line message.

    Advisory prose runs to paragraphs and our `message` is a line. The full
    text stays in the evidence, so nothing is lost by leading with the part a
    reader needs to decide whether to open the link.
    """
    first = description.strip().split("\n", 1)[0].strip()
    if len(first) <= 160:
        return first
    return first[:157].rstrip() + "..."


def as_finding(advisory: Advisory, claimed: Claimed, availability: Availability) -> Finding:
    """One advisory as a finding.

    `ClaimTable.as_finding` is not used here, and the reason is worth stating.
    It gives every finding of a code the same title, message and references,
    which is right for a linter -- every `S324` is the same observation -- and
    wrong for a vulnerability, where the title *is* the content. It would also
    give all of them one rule id, and five advisories against one pinned line
    would then share a fingerprint and collapse to one entry in a baseline.
    """
    if claimed.family is None or claimed.severity is None:
        raise ValueError(f"{claimed.code}: an adopted code needs a family and a severity")
    fixes = ", ".join(advisory.fix_versions)
    remediation = claimed.remediation
    if advisory.fix_versions:
        remediation = f"Upgrade {advisory.package} to {fixes}. {claimed.remediation}"
    return Finding(
        rule_id=f"PIP-AUDIT-{advisory.identifier}",
        title=f"{advisory.package} {advisory.version} is affected by {advisory.identifier}",
        family=claimed.family,
        severity=claimed.severity,
        confidence=claimed.confidence,
        tier=Tier.STATIC,
        location=Location(file=advisory.file, line=advisory.line, snippet=advisory.raw),
        message=summarise(advisory.description) or f"{advisory.identifier} affects this version",
        rationale=claimed.why,
        remediation=remediation,
        evidence=(
            Evidence(
                kind=EvidenceKind.COMMAND_OUTPUT,
                content=(
                    f"{advisory.identifier} affects {advisory.package} {advisory.version}"
                    + (f"; fixed in {fixes}" if fixes else "; no fixed version is published")
                    + (f"\n{advisory.description}" if advisory.description else "")
                ),
                source=availability.describe(),
            ),
        ),
        references=tuple(
            f"https://osv.dev/vulnerability/{identifier}"
            for identifier in (advisory.identifier, *advisory.aliases)
        ),
        properties={
            "external": availability.tool,
            "code": advisory.source,
            "package": advisory.package,
            "version": advisory.version,
        },
    )


@dataclass(frozen=True, slots=True)
class PipAuditAdapter:
    """Audits the versions a project pins, without installing any of them."""

    executable: str = TOOL

    @property
    def name(self) -> str:
        return TOOL

    @property
    def table(self) -> ClaimTable:
        return CLAIMS

    def probe(self) -> Availability:
        return probe_tool(self.executable, name=TOOL)

    def collect(self, root: object) -> Report:
        """Never raises for anything pip-audit does.

        One invocation per production manifest, because a finding has to cite
        the file that pinned the version and a combined run could not say which
        of two files a package came from.

        This is the one adapter that reaches the network -- a vulnerability
        database is not something we can carry -- so it is only ever run when
        the user asks for it, and its tests read recorded output.
        """
        directory = Path(str(root))
        availability = self.probe()
        if not availability:
            return Report(tool=TOOL, availability=availability)

        findings: list[Finding] = []
        diagnostics: list[str] = []
        unclaimed: set[str] = set()
        declared = by_file(m for m in manifest.discover(directory) if not m.development)
        if not declared:
            return Report(
                tool=TOOL,
                availability=availability,
                diagnostics=("no production dependency manifest was found",),
            )

        for path, requirements in declared.items():
            auditable, skipped = pinned(requirements)
            where = path.relative_to(directory).as_posix()
            if skipped:
                shown = ", ".join(sorted(skipped)[:3])
                more = ", ..." if len(skipped) > 3 else ""
                diagnostics.append(
                    f"{where}: {len(skipped)} of {len(requirements)} requirements are not "
                    f"pinned to an exact version and were not audited ({shown}{more})"
                )
            if not auditable:
                continue
            advisories, problem = self.run(auditable, requirements, where, directory)
            if problem is not None:
                diagnostics.append(f"{where}: {problem}")
                continue
            for advisory in advisories:
                decision = CLAIMS.by_code.get(advisory.source)
                if decision is None:
                    unclaimed.add(advisory.source)
                elif decision.adopted:
                    findings.append(as_finding(advisory, decision, availability))

        return Report(
            tool=TOOL,
            findings=tuple(findings),
            availability=availability,
            unclaimed=tuple(sorted(unclaimed)),
            diagnostics=tuple(diagnostics),
        )

    def run(
        self,
        auditable: list[str],
        requirements: tuple[manifest.Requirement, ...],
        relative: str,
        root: Path,
    ) -> tuple[tuple[Advisory, ...], str | None]:
        """One pip-audit run over a requirements file we wrote.

        Both files are ours rather than the target's. The input holds only the
        lines we chose, so a read-only checkout still works; the output goes to
        a file for the reason the ruff adapter learned the hard way, that
        `live.runner` caps a captured stream at 1 MiB and a JSON report
        truncated mid-object parses as nothing at all.
        """
        with tempfile.TemporaryDirectory(prefix="djaudit-pip-audit-") as scratch:
            written = Path(scratch) / "requirements.txt"
            written.write_text("\n".join(auditable) + "\n", encoding="utf-8")
            destination = Path(scratch) / "advisories.json"
            outcome = run_tool(
                [self.executable, "-r", str(written), "-o", str(destination), *ARGUMENTS],
                root=root,
            )
            try:
                payload = destination.read_text(encoding="utf-8")
            except OSError:
                payload = ""
        if not payload.strip():
            return (), outcome.describe()
        try:
            return parse(payload, requirements, relative), None
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            return (), f"{outcome.describe()}; its output could not be read: {exc}"

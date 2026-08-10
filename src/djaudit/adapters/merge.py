"""Folding external reports into a run's own findings.

The plan described this substep as collapsing "the same file, the same line, the
same underlying issue reported by two tools" into one finding carrying both as
evidence. The corpus says that situation does not arise, and that trying to
create it would lose real findings. Both halves were measured before anything
here was written.

**Across tools there are no collisions to collapse.** Over healthchecks, netbox
and pretix -- 268 findings of ours against 41 the ruff adapter adopts -- not one
external finding lands on a file and line that any djaudit finding also names.
Twelve share a file and none share a line. That is not luck: overlap is settled
one layer earlier, by the claim table, which rules on a tool's *rule codes*
before any of them become findings. A code we cover is `SUBSUMED` and never
reaches this function. Deciding overlap once per code, in writing, with a
reason, beats guessing at it per location in a report -- and the guess would
have to be re-made on every run.

The two adapters shipped cannot collide with each other either, and for a
structural reason rather than a lucky one: ruff reports on Python source and
pip-audit reports on dependency manifests, which are disjoint sets of files.

**Within a run, findings that share a location are usually not duplicates.**
Nine corpus locations carry more than one finding, and every one of them is two
genuinely different problems:

- `serializers/order.py:1393` carries two `DJP-001`s -- `cp.variation` and
  `cp.item` are two unprefetched attributes on one line, at columns 30 and 48,
  and each is a separate query per row.
- `views/order.py:1057` carries two `DJA-014`s, because one `Meta` is shared by
  `EventOrderPositionViewSet` and `OrganizerOrderPositionViewSet` and each
  exposes the filter.
- `settings.py:1` carries five `DJS` findings, since a settings module's
  problems are all attributed to the module.

Collapsing by location would have deleted a finding at every one of them. So
deduplication here is by **fingerprint**, which is what identity already means
everywhere else in djaudit: it is what the baseline matches on, and two findings
that share one are the same finding by definition. Location is deliberately not
part of the test.

Adapters build findings without fingerprints, because an occurrence index only
means something across a whole set and an adapter holds one tool's part of one.
Assigning them is therefore this module's job, and it is done per group so that
a tool reporting the same text twice keeps two findings while two tools
reporting one thing keep one.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from djaudit import fingerprint as fp
from djaudit.adapters.base import Report
from djaudit.models import Finding


@dataclass(frozen=True, slots=True)
class Merged:
    """A run's findings once external reports have been folded in."""

    findings: tuple[Finding, ...] = ()
    diagnostics: tuple[str, ...] = ()
    notices: tuple[str, ...] = ()
    """One line per adapter, whether it ran or not.

    An adapter that was asked for and could not run has to leave a trace. A
    report that is short because a tool was missing looks exactly like a report
    that is short because a project is clean, and the two are not the same
    situation.
    """

    duplicates: int = 0
    """External findings dropped for sharing a fingerprint with one already held.

    Counted rather than inferred. Zero because nothing collided and zero because
    no adapter ran are different answers, and a reader is entitled to tell them
    apart.
    """


def identified(findings: Sequence[Finding]) -> list[Finding]:
    """The same findings, each carrying a fingerprint.

    Adapters do not fingerprint what they build, and nothing asks them to: a
    fingerprint's occurrence index is only meaningful across a whole set, and an
    adapter holds one tool's part of one. So it is assigned here, per group,
    which is what makes two findings that differ in nothing but their position
    on a line distinguishable at all.

    Findings that already carry one are returned untouched. The engine assigns
    ours over the set it kept, and re-deriving them from a different set could
    hand a finding a different occurrence index and a different fingerprint --
    which would quietly invalidate every baseline entry written for it.
    """
    if all(f.fingerprint for f in findings):
        return list(findings)
    if any(f.fingerprint for f in findings):
        raise ValueError(
            "some of these findings are fingerprinted and some are not; "
            "re-deriving the set would change identities that a baseline may hold"
        )
    return fp.assign(findings)


def merge(ours: Sequence[Finding], reports: Iterable[Report]) -> Merged:
    """Combine our findings with every external report, in one sorted order.

    Ours are added first, so a fingerprint collision resolves in favour of the
    finding that carries our own rationale, remediation and evidence rather than
    a linter's one-line message. Today no such collision is reachable -- rule
    ids are namespaced per tool and the fingerprint is built from the rule id --
    but the rule is stated here rather than left to be discovered later by
    whoever adds the third adapter.

    Diagnostics and notices are kept apart on purpose. A diagnostic is something
    that went wrong inside a tool that ran; a notice is the standing fact of
    whether it ran at all, which is reported even when everything worked.
    """
    combined: list[Finding] = []
    seen: set[str] = set()
    diagnostics: list[str] = []
    notices: list[str] = []
    duplicates = 0

    for finding in identified(ours):
        if finding.fingerprint in seen:
            continue
        seen.add(finding.fingerprint)
        combined.append(finding)

    for report in reports:
        notices.append(report.explain())
        diagnostics.extend(f"{report.tool}: {problem}" for problem in report.diagnostics)
        if report.unclaimed:
            # Not a diagnostic about a failure -- the tool worked. It is a
            # decision we have not made, and the adapter's claim table is the
            # place it has to be made, so name the codes rather than the count.
            diagnostics.append(
                f"{report.tool}: {len(report.unclaimed)} rule "
                f"{'code' if len(report.unclaimed) == 1 else 'codes'} reported but "
                f"never ruled on: {', '.join(report.unclaimed)}"
            )
        for finding in identified(report.findings):
            if finding.fingerprint in seen:
                duplicates += 1
                continue
            seen.add(finding.fingerprint)
            combined.append(finding)

    return Merged(
        findings=tuple(sorted(combined, key=lambda f: f.sort_key)),
        diagnostics=tuple(diagnostics),
        notices=tuple(notices),
        duplicates=duplicates,
    )

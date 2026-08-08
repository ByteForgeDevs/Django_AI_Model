"""Where Django's deployment check and our static rules say the same thing.

Both tiers look at settings and both find real defects, so on a live run the
same misconfiguration is available twice. Reporting it twice would be the most
obvious kind of noise -- and picking one arbitrarily would throw away whichever
half the reader needed.

**Each half knows something the other cannot.** Our rule read the source, so it
has a `file:line` and can name the assignment to change; Django's check reports
`?` for an object, because by the time it runs, settings are values and the
file they came from is gone. Django resolved those values for real, through
every import, override and environment variable, so it knows what the setting
*is*; our rule can only know what the source says, and emits `tentative` when
the source does not settle it.

So the two are merged rather than deduplicated: our finding keeps its location
and gains Django's verdict as evidence, and a finding Django confirms is raised
to `certain`.

**Confirmation raises confidence and silence never lowers it.** Django's checks
are narrower than ours -- it has nothing to say about CORS, fast password
hashers, or a `SECURE_PROXY_SSL_HEADER` that trusts a client header -- and
`SILENCED_SYSTEM_CHECKS` can remove a check with no trace but a number. Reading
"Django did not mention it" as "Django disagrees" would let a project silence
our findings by silencing Django's, which is exactly backwards.

The mapping is many-to-one and that is Django's doing, not ours: whether an
insecure session cookie is `security.W010`, `W011` or `W012` depends on whether
sessions are enabled through `INSTALLED_APPS`, through `MIDDLEWARE`, or are
simply configured insecurely. All three are one defect and one line to fix.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import NamedTuple

from djaudit.live.checks import Report
from djaudit.live.checks import Unknown as ChecksUnknown
from djaudit.models import Confidence, Evidence, EvidenceKind, Finding

CORROBORATES: Mapping[str, str] = {
    "security.W004": "DJS-007",
    "security.W005": "DJS-008",
    "security.W006": "DJS-018",
    "security.W008": "DJS-006",
    "security.W009": "DJS-003",
    "security.W010": "DJS-009",
    "security.W011": "DJS-009",
    "security.W012": "DJS-009",
    "security.W013": "DJS-011",
    "security.W014": "DJS-011",
    "security.W015": "DJS-011",
    "security.W016": "DJS-010",
    "security.W002": "DJS-017",
    "security.W019": "DJS-017",
    "security.W018": "DJS-001",
    "security.W020": "DJS-013",
}
"""Django's check ids against the rule of ours that reports the same defect.

Read out of ``django/core/checks/security/`` rather than matched by title.
Several ids map to one rule because Django distinguishes *how* a setting came
to be wrong -- `W010`, `W011` and `W012` are one insecure session cookie
reported three ways -- and the reader has one line to change either way.

Django checks with no entry here are not oversights; they are the recall gap
`DJS-028` reports. `security.W001` and `W003` are about missing middleware,
`W021` and `W022` about headers we do not yet rule on.
"""


class Corroboration(NamedTuple):
    """The findings after merging, and what Django said that we did not."""

    findings: tuple[Finding, ...]
    confirmed: frozenset[str]
    """Django check ids that landed on a finding of ours."""

    unclaimed: frozenset[str]
    """Security checks Django reported that landed on no finding of ours.

    Either we have no rule for it, or we have one and it did not fire on this
    project. `DJS-028` reports both, because from the reader's side they are
    the same sentence: Django found something here and we did not.
    """

    @property
    def merged(self) -> int:
        return len(self.confirmed)


def corroborate(
    findings: Iterable[Finding],
    report: Report | ChecksUnknown,
) -> Corroboration:
    """Fold Django's deployment check into our own findings.

    A run without a usable report returns the findings untouched, which is the
    only safe answer: an unreachable check is not a disagreement.
    """
    kept = tuple(findings)
    if not isinstance(report, Report):
        # Mutation testing reports this guard as a survivor and it is right:
        # `Unknown` answers nothing to every question, so falling through would
        # produce the same empty result. It is kept because it is what narrows
        # the type -- `_confirm` below takes a `Report` and cannot be handed a
        # failure to look, which is a stronger statement than a runtime check.
        return Corroboration(kept, frozenset(), frozenset())

    by_rule: dict[str, list[str]] = {}
    for message in report.messages:
        if message.id is None:
            continue
        rule = CORROBORATES.get(message.id)
        if rule is not None:
            by_rule.setdefault(rule, []).append(message.id)

    confirmed: set[str] = set()
    merged: list[Finding] = []
    for finding in kept:
        ids = by_rule.get(finding.rule_id)
        if ids is None:
            merged.append(finding)
            continue
        confirmed.update(ids)
        merged.append(_confirm(finding, report, ids))

    # A gap is a check that landed on nothing of ours, which is a wider and
    # more useful set than "a check we have no rule for". A rule can exist and
    # still miss: `DJS-006` reads `SECURE_SSL_REDIRECT` out of the source and
    # cannot settle it when it is assembled from the environment, and Django,
    # which sees the resolved value, can. That is a recall gap too, and it is
    # the more interesting one because the fix is ours rather than unwritten.
    unclaimed = frozenset(
        check for check in report.ids() if check.startswith("security.") and check not in confirmed
    )
    return Corroboration(tuple(merged), frozenset(confirmed), unclaimed)


def _confirm(finding: Finding, report: Report, ids: list[str]) -> Finding:
    """Add Django's verdict to a finding of ours and raise its confidence.

    The evidence is Django's own sentence, quoted rather than paraphrased: it
    is the part of the finding the reader can check against a command they can
    run themselves.
    """
    # Every id here was read off `report.messages` a moment ago, so each lookup
    # finds its message. An `if not quoted: return finding` fallback was written
    # first and removed: it was unreachable, and unreachable code reads as an
    # untested branch forever.
    quoted = [m.describe() for check in sorted(ids) if (m := report.by_id(check)) is not None]
    evidence = Evidence(
        kind=EvidenceKind.COMMAND_OUTPUT,
        content="\n".join(quoted),
        source="manage.py check --deploy",
    )
    return replace(
        finding,
        confidence=Confidence.CERTAIN,
        evidence=(*finding.evidence, evidence),
        message=(
            f"{finding.message} Django's own deployment check agrees, "
            f"reporting {_and_list(sorted(ids))}."
        ),
    )


def _and_list(items: list[str]) -> str:
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} and {items[-1]}"

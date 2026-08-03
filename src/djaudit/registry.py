"""Rule metadata, the rule base class, and the global registry."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache
from threading import Lock
from typing import TYPE_CHECKING, ClassVar

from djaudit.models import Confidence, Evidence, Family, Finding, Location, Severity, Tier

if TYPE_CHECKING:
    from djaudit.context import ProjectContext

RULE_ID_PATTERN = re.compile(r"^(DJS|DJI|DJA|DJP|DJM|DJX)-\d{3}$")


class RuleError(Exception):
    """Raised when a rule is declared incorrectly."""


@dataclass(frozen=True, slots=True)
class RuleMeta:
    """Static description of a rule.

    Severity and confidence here are the rule's *defaults*. A rule may downgrade
    or escalate per finding -- for example ``DEBUG = True`` is certain and
    critical in a production settings module, but merely firm in a shared base
    module that a production module might override.
    """

    id: str
    title: str
    family: Family
    severity: Severity
    confidence: Confidence
    tier: Tier
    rationale: str
    remediation: str
    references: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    """What this rule cannot see, in the reader's terms.

    Every rule here reasons about source and none of them can see a deployment,
    so each one has a boundary where its claim stops. Those boundaries are
    already encoded in the ``ceiling`` each rule sets, but a confidence level
    is a number and a number does not tell somebody staring at a finding *why*
    it might not apply to them. Written next to the rule so it goes stale in
    the same commit that makes it wrong, and rendered into ``docs/rules``.
    """


class Rule(ABC):
    """Base class for all rules.

    Subclasses declare ``meta`` and implement ``check``. The ``finding`` helper
    builds a :class:`Finding` from ``meta`` so rule bodies stay focused on
    detection rather than on assembling result objects.
    """

    meta: ClassVar[RuleMeta]

    @abstractmethod
    def check(self, ctx: ProjectContext) -> Iterator[Finding]:
        """Yield findings for the given project."""
        raise NotImplementedError

    def finding(
        self,
        *,
        location: Location,
        message: str,
        evidence: tuple[Evidence, ...] = (),
        severity: Severity | None = None,
        confidence: Confidence | None = None,
        rationale: str | None = None,
        remediation: str | None = None,
        properties: dict[str, str] | None = None,
    ) -> Finding:
        meta = self.meta
        return Finding(
            rule_id=meta.id,
            title=meta.title,
            family=meta.family,
            severity=severity or meta.severity,
            confidence=confidence or meta.confidence,
            tier=meta.tier,
            location=location,
            message=message,
            rationale=rationale if rationale is not None else meta.rationale,
            remediation=remediation if remediation is not None else meta.remediation,
            evidence=evidence,
            references=meta.references,
            properties=properties or {},
        )


_REGISTRY: dict[str, type[Rule]] = {}


def register(rule_cls: type[Rule]) -> type[Rule]:
    """Class decorator adding a rule to the global registry.

    Enforces the invariants that make the catalogue navigable: well-formed ids,
    no duplicates, and a family prefix that matches the declared family.
    """
    meta = getattr(rule_cls, "meta", None)
    if not isinstance(meta, RuleMeta):
        raise RuleError(f"{rule_cls.__name__} must declare a RuleMeta as 'meta'")
    if not RULE_ID_PATTERN.match(meta.id):
        raise RuleError(f"{meta.id!r} is not a valid rule id (expected e.g. 'DJS-001')")
    if not meta.id.startswith(meta.family.value):
        raise RuleError(f"rule {meta.id} declares family {meta.family.value}, which its id ignores")
    if meta.id in _REGISTRY:
        raise RuleError(f"duplicate rule id {meta.id} ({rule_cls.__name__})")
    _REGISTRY[meta.id] = rule_cls
    return rule_cls


def all_rules() -> list[type[Rule]]:
    """Every registered rule, ordered by id."""
    _load_builtin_rules()
    return [_REGISTRY[rule_id] for rule_id in sorted(_REGISTRY)]


def get(rule_id: str) -> type[Rule]:
    _load_builtin_rules()
    try:
        return _REGISTRY[rule_id]
    except KeyError as exc:
        raise RuleError(f"unknown rule id: {rule_id}") from exc


def select(
    *,
    families: set[Family] | None = None,
    tiers: set[Tier] | None = None,
    include: set[str] | None = None,
    exclude: set[str] | None = None,
) -> list[type[Rule]]:
    """Filter the catalogue. ``include`` wins over every other filter."""
    rules = all_rules()
    if include:
        return [r for r in rules if r.meta.id in include]
    if families is not None:
        rules = [r for r in rules if r.meta.family in families]
    if tiers is not None:
        rules = [r for r in rules if r.meta.tier in tiers]
    if exclude:
        rules = [r for r in rules if r.meta.id not in exclude]
    return rules


_loader_lock = Lock()


@lru_cache(maxsize=1)
def _load_builtin_rules() -> None:
    """Import built-in rule modules so their decorators run.

    Done lazily rather than at package import so that importing ``djaudit``
    stays cheap for consumers that only want the schema, and cached so repeated
    lookups do not re-walk the package.
    """
    with _loader_lock:
        from djaudit.rules import load_all  # noqa: PLC0415 - deferred to break the import cycle

        load_all()

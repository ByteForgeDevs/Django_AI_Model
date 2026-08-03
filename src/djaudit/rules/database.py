"""DJS -- what the ``DATABASES`` dictionary actually says.

Every other settings rule in this phase reads a value. These read a nested
mapping, and that difference drives the whole module.

Two things make it awkward. The first is that a real ``DATABASES`` block always
contains something from the environment, so the setting as a whole resolves to
unknown and a rule waiting for it to resolve would never fire; ``entries()``
exists for that and is used twice, once per level. The second is that projects
assign ``DATABASES`` more than once -- Healthchecks writes it three times, once
plainly and twice inside ``if`` statements keyed on an environment variable --
so the value the resolver settles on is the last branch rather than the one
that will run. Reading only that would inspect the MySQL block of a project
deployed on Postgres.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass

from djaudit.context import ProjectContext
from djaudit.models import Confidence, Evidence, EvidenceKind, Family, Finding, Severity, Tier
from djaudit.registry import RuleMeta, register
from djaudit.rules._base import (
    Entry,
    SettingGroup,
    SettingsRule,
    entries,
    literal_text,
)
from djaudit.settings import Definition, ResolvedSetting, SettingsView

_DATABASES_DOCS = "https://docs.djangoproject.com/en/stable/ref/settings/#databases"

SQLITE = "sqlite"


@dataclass(frozen=True)
class DatabaseConfig:
    """One alias's mapping, as written by one assignment."""

    alias: str
    settings: dict[str, Entry]
    node: ast.expr | None
    definition: Definition
    conditional: bool
    """Whether the assignment that wrote it sits inside an ``if``.

    A conditional block is one deployment shape among several rather than the
    configuration, which is worth saying in a finding and worth knowing when
    deciding whether a sibling branch has already answered the question.
    """

    def get(self, key: str) -> Entry | None:
        return self.settings.get(key)

    def text(self, key: str) -> str | None:
        """A key's value as a string, if it is one."""
        entry = self.settings.get(key)
        return None if entry is None else literal_text(entry.value)

    def options(self, view: SettingsView) -> dict[str, Entry]:
        """The nested ``OPTIONS`` mapping, read one key at a time."""
        entry = self.settings.get("OPTIONS")
        if entry is None:
            return {}
        return entries(view, entry.node, entry.value)

    @property
    def engine(self) -> str | None:
        return self.text("ENGINE")

    @property
    def is_sqlite(self) -> bool:
        engine = self.engine
        return engine is not None and SQLITE in engine

    def at(self) -> ast.expr | None:
        """Where to point a finding that is about the alias as a whole."""
        return self.node


def live_definitions(resolved: ResolvedSetting) -> tuple[Definition, ...]:
    """The assignments that can still decide the value.

    An unconditional assignment replaces everything before it outright, so
    anything earlier is dead code and reporting it would be reporting a value
    that never reaches a connection. Everything from the last unconditional
    assignment onward is live: the unconditional one is the fallback and each
    conditional one after it is a deployment that overrides it.
    """
    definitions = resolved.definitions
    last_plain = 0
    for index, definition in enumerate(definitions):
        if not definition.conditional:
            last_plain = index
    return definitions[last_plain:]


def database_configs(
    view: SettingsView, resolved: ResolvedSetting
) -> dict[str, tuple[DatabaseConfig, ...]]:
    """Every configuration each alias could have, in source order.

    Keyed by alias because an alias is a separate server with separate
    settings, and grouped rather than flattened because the rules here have to
    answer "did *any* branch get this right", which is not a question a single
    branch can answer.
    """
    found: dict[str, list[DatabaseConfig]] = {}
    for definition in live_definitions(resolved):
        node = definition.node
        if not isinstance(node, ast.Assign | ast.AnnAssign) or node.value is None:
            continue
        for alias, entry in entries(view, node.value).items():
            found.setdefault(alias, []).append(
                DatabaseConfig(
                    alias=alias,
                    settings=entries(view, entry.node, entry.value),
                    node=entry.node,
                    definition=definition,
                    conditional=definition.conditional,
                )
            )
    return {alias: tuple(configs) for alias, configs in found.items()}


class DatabaseAliasRule(SettingsRule):
    """A rule about the contents of one ``DATABASES`` alias.

    Subclasses answer per alias rather than per assignment. That is the whole
    reason this base exists: a project with three ``DATABASES`` blocks has one
    database, and a rule reporting each branch separately would report one
    decision three times.
    """

    setting = "DATABASES"

    def inspect(self, ctx: ProjectContext, group: SettingGroup) -> Iterator[Finding]:
        if not group.setting.is_assigned:
            return
        view = self.views[group.module.dotted]
        for alias, configs in database_configs(view, group.setting).items():
            yield from self.judge(ctx, group, alias, configs)

    def judge(
        self,
        ctx: ProjectContext,
        group: SettingGroup,
        alias: str,
        configs: tuple[DatabaseConfig, ...],
    ) -> Iterator[Finding]:
        raise NotImplementedError


@register
class ConnectionsNotReused(DatabaseAliasRule):
    """Every request opens and closes its own database connection.

    The narrowest rule in this family on purpose, because the broad version is
    noise: ``CONN_MAX_AGE`` at 0 is Django's default, so unqualified it fires
    on nearly every project ever written. What makes it worth reporting is the
    cost of the connection being thrown away, so SQLite is excluded -- opening
    a file is not a handshake, and Django's own documentation warns that
    persistent connections there cause locking problems rather than solving
    anything -- as is a project that configured a connection pool, where 0 is
    not merely acceptable but required.
    """

    ceiling = Confidence.FIRM
    """We can read the setting, but not the deployment. A connection pooler
    sitting in front of Postgres makes 0 the correct value and is invisible
    from here."""

    caveats = (
        "an external connection pooler such as pgbouncer makes this the correct "
        "value, and nothing in the settings would show one",
    )

    def judge(
        self,
        ctx: ProjectContext,
        group: SettingGroup,
        alias: str,
        configs: tuple[DatabaseConfig, ...],
    ) -> Iterator[Finding]:
        view = self.views[group.module.dotted]
        readable = [config for config in configs if config.engine is not None]
        if not readable or all(config.is_sqlite for config in readable):
            return
        if len(readable) != len(configs):
            # A branch whose engine we could not read might be the one that
            # runs, and it might be SQLite or it might already be fixed.
            return

        for config in configs:
            if config.is_sqlite:
                continue
            if config.options(view).get("pool") is not None:
                # Django refuses to start with both a pool and a non-zero
                # CONN_MAX_AGE, so a pool is a deliberate answer to this.
                return
            entry = config.get("CONN_MAX_AGE")
            if entry is None:
                continue
            if not entry.value.is_always(0):
                # Any branch that reuses connections settles the question.
                return

        offender = next(
            (config for config in configs if not config.is_sqlite),
            configs[0],
        )
        entry = offender.get("CONN_MAX_AGE")
        assigned = entry is not None
        where = group.module.dotted or ctx.rel(group.module.path)
        yield self.report(
            ctx,
            group.narrow(f'DATABASES["{alias}"]["CONN_MAX_AGE"]', group.setting.value),
            at=(entry.node if entry is not None else offender.at()),
            message=(
                f"the {alias!r} database in {where}{group.describe_reach()} "
                + ("sets CONN_MAX_AGE to 0" if assigned else "never sets CONN_MAX_AGE")
                + ", so Django opens a new connection for every request and closes it "
                "again at the end -- on Postgres that is a TCP handshake, a TLS "
                "handshake and an authentication round trip added to the latency of "
                "every page, paid before any query runs"
            ),
            evidence=(
                Evidence(
                    kind=EvidenceKind.CONFIG,
                    content=(
                        f"ENGINE = {offender.engine}\n"
                        f"CONN_MAX_AGE = {'0 (Django default)' if not assigned else '0'}"
                    ),
                    source="djaudit settings resolver",
                ),
            ),
        )

    meta = RuleMeta(
        id="DJS-021",
        title="database connections are not reused between requests",
        family=Family.DJS,
        severity=Severity.LOW,
        confidence=Confidence.FIRM,
        tier=Tier.STATIC,
        rationale=(
            "Django's default is to close the database connection at the end of every "
            "request. For SQLite that costs nothing, which is why the default is what "
            "it is, but for a networked database it means each request pays a TCP "
            "handshake, a TLS handshake and an authentication round trip before its "
            "first query runs -- and on a managed Postgres instance, where the database "
            "is a few milliseconds away rather than on localhost, that is routinely a "
            "larger share of response time than the queries themselves. It is also the "
            "cheapest fix available: one integer, no code change, no schema change. The "
            "reason it goes unnoticed is that it is invisible in development, where the "
            "database is a local socket and the handshake is free."
        ),
        remediation=(
            "Set CONN_MAX_AGE inside the database alias -- not at module level, where "
            "Django never reads it -- to a value below whatever idle timeout sits "
            "between the application and the database, commonly 60. Set "
            "CONN_HEALTH_CHECKS to True alongside it so a connection dropped while "
            "idle is replaced rather than raising on first use. If an external pooler "
            "such as pgbouncer already handles this, leave CONN_MAX_AGE at 0 and "
            "suppress the finding; if you are on Django 5.1 or later with psycopg 3, "
            "consider OPTIONS['pool'] instead, which supersedes this setting entirely."
        ),
        references=(
            _DATABASES_DOCS,
            "https://docs.djangoproject.com/en/stable/ref/databases/#persistent-database-connections",
            "https://docs.djangoproject.com/en/stable/ref/databases/#connection-pool",
        ),
    )

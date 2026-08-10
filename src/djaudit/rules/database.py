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

from djaudit.context import ProjectContext
from djaudit.engines import DatabaseConfig, database_configs
from djaudit.models import Confidence, Evidence, EvidenceKind, Family, Finding, Severity, Tier
from djaudit.registry import RuleMeta, register
from djaudit.rules._base import (
    SettingGroup,
    SettingsRule,
)
from djaudit.settings import (
    Entry,
    SettingsView,
    literal_text,
)

_DATABASES_DOCS = "https://docs.djangoproject.com/en/stable/ref/settings/#databases"


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
        limitations=(
            "A connection pooler in front of Postgres makes 0 the correct value and is "
            "invisible from here. So is a deployment whose worker model makes persistent "
            "connections harmful.",
        ),
    )


_SSL_DOCS = "https://www.postgresql.org/docs/current/libpq-ssl.html"

POSTGRES_ENGINES = ("postgresql", "postgis", "psqlextra", "postgres")
"""Substrings that mark a Postgres backend.

Matched loosely on purpose. django.contrib.gis.db.backends.postgis,
django_db_geventpool and psqlextra all wrap the same libpq connection and take
the same OPTIONS, and a rule that only recognised the stock backend would go
quiet on exactly the projects most likely to be running a real deployment.
"""

UNPROTECTED_SSLMODES = {"disable", "allow", "prefer"}
"""libpq modes that will speak plaintext.

`prefer` is the default and the dangerous one: it asks for TLS, accepts a
refusal without complaint, and reports nothing either way, so a downgrade is
indistinguishable from a working connection.
"""

LOCAL_HOSTS = frozenset({"", "localhost", "127.0.0.1", "::1"})


def definitely_local(entry: Entry | None) -> bool:
    """Whether the connection certainly never crosses a network.

    An absent or empty HOST means a Unix domain socket, where libpq ignores
    sslmode entirely, and a leading slash is the socket directory spelled out.
    Loopback is included because an attacker positioned to read it has already
    won. Anything env-dependent is not "certainly" anything: the literal we can
    see is the fallback, and the deployment that matters is the one that sets
    the variable.
    """
    if entry is None:
        return True
    if entry.value.env_dependent:
        return False
    text = literal_text(entry.value)
    if text is None:
        return False
    return text in LOCAL_HOSTS or text.startswith("/")


@register
class PostgresWithoutTls(DatabaseAliasRule):
    """A Postgres connection that will accept an unencrypted session.

    Unlike DJS-021, one bad branch is enough. That rule asks whether anyone
    thought about a setting, so any branch answering it settles the question.
    This one asks whether a deployment exists that talks to the database in the
    clear, and a second branch doing it properly does not un-expose the first.
    """

    ceiling = Confidence.FIRM
    """libpq also reads sslmode from PGSSLMODE and from a service file, and the
    network between the application and the database might be one nobody else
    can reach. The setting is read exactly; what it means for a given
    deployment is the inference."""

    caveats = (
        "libpq also honours PGSSLMODE and ~/.pg_service.conf, which could raise this "
        "at run time without appearing in the settings",
    )

    def judge(
        self,
        ctx: ProjectContext,
        group: SettingGroup,
        alias: str,
        configs: tuple[DatabaseConfig, ...],
    ) -> Iterator[Finding]:
        view = self.views[group.module.dotted]
        for config in configs:
            engine = config.engine
            if engine is None or not any(name in engine for name in POSTGRES_ENGINES):
                continue
            if definitely_local(config.get("HOST")):
                continue

            options_entry = config.get("OPTIONS")
            options = config.options(view)
            if options_entry is not None and not options:
                # OPTIONS built by a helper or spread from another dict. The
                # sslmode could be in there and we would never know.
                continue

            mode_entry = options.get("sslmode")
            mode = None if mode_entry is None else literal_text(mode_entry.value)
            if mode is not None and mode not in UNPROTECTED_SSLMODES:
                continue
            if mode_entry is not None and mode is None:
                continue

            where = group.module.dotted or ctx.rel(group.module.path)
            stated = (
                f'sets sslmode to "{mode}"'
                if mode
                else 'never sets sslmode, so libpq defaults to "prefer"'
            )
            yield self.report(
                ctx,
                group.narrow(f'DATABASES["{alias}"]["OPTIONS"]["sslmode"]', group.setting.value),
                at=(mode_entry.node if mode_entry is not None else config.at()),
                severity=(Severity.HIGH if mode == "disable" else Severity.MEDIUM),
                message=(
                    f"the {alias!r} Postgres connection in {where}"
                    f"{group.describe_reach()} {stated}, so the session can run "
                    "unencrypted over the network -- and because libpq downgrades "
                    "silently, a connection carrying the database password in the "
                    "clear looks exactly like one that did not"
                ),
                evidence=(
                    Evidence(
                        kind=EvidenceKind.CONFIG,
                        content=(
                            f"ENGINE = {engine}\n"
                            f"sslmode = {mode or 'unset (libpq default: prefer)'}"
                        ),
                        source="djaudit settings resolver",
                    ),
                ),
            )
            return

    meta = RuleMeta(
        id="DJS-022",
        title="Postgres connection permits an unencrypted session",
        family=Family.DJS,
        severity=Severity.MEDIUM,
        confidence=Confidence.FIRM,
        tier=Tier.STATIC,
        rationale=(
            "libpq defaults sslmode to `prefer`, which is the worst possible default "
            "to inherit by accident: it asks the server for TLS, accepts a refusal "
            "without complaint, and reports nothing either way. A connection that has "
            "silently fallen back to plaintext is indistinguishable from an encrypted "
            "one from inside the application, so nothing will ever surface the "
            "problem -- and an attacker who can answer the connection first does not "
            "need to break anything, only to say no. What travels over that session "
            "is the database password on the way in and every row on the way back. "
            "The reason this survives review is that it is invisible in development, "
            "where the database is a Unix socket and sslmode genuinely does not "
            "matter, and stays invisible in production because everything still works."
        ),
        remediation=(
            "Set OPTIONS['sslmode'] on the alias. `require` encrypts the session and "
            "is the minimum worth having, but it does not check who answered, so it "
            "stops passive capture and not an active attacker; `verify-full` with "
            "OPTIONS['sslrootcert'] pointed at the CA is the setting that actually "
            "authenticates the server, and is what a managed Postgres provider's own "
            "documentation will tell you to use. If the database is reached over a "
            "Unix socket, say so by leaving HOST empty rather than pointing it at "
            "localhost, which is a TCP connection and does need TLS."
        ),
        references=(
            _SSL_DOCS,
            "https://docs.djangoproject.com/en/stable/ref/databases/#postgresql-notes",
            "https://cwe.mitre.org/data/definitions/319.html",
        ),
        limitations=(
            "The libpq client also reads sslmode from PGSSLMODE and from a service file, "
            "neither of which is in the repository. The setting is read exactly; what it means "
            "for a deployment is the inference.",
            "A database reached over a Unix socket or a private link may not need TLS at all, "
            "and the host is usually an environment variable we cannot resolve.",
        ),
    )


PER_ALIAS_KEYS: dict[str, tuple[Severity, str]] = {
    "ATOMIC_REQUESTS": (
        Severity.MEDIUM,
        "every view is wrapped in a transaction and rolls back as a unit when it "
        "raises, when in fact each statement commits on its own and a view that "
        "fails halfway leaves half its writes behind",
    ),
    "AUTOCOMMIT": (
        Severity.MEDIUM,
        "transactions are being managed explicitly, when Django is in fact "
        "committing after every statement",
    ),
    "CONN_MAX_AGE": (
        Severity.LOW,
        "connections are reused between requests, when in fact every request still "
        "opens and closes its own",
    ),
    "CONN_HEALTH_CHECKS": (
        Severity.LOW,
        "a connection dropped while idle is replaced before it is used, when in "
        "fact the first query on it raises",
    ),
    "DISABLE_SERVER_SIDE_CURSORS": (
        Severity.LOW,
        "server-side cursors are switched off, which is the setting a connection "
        "pooler in transaction mode needs, when in fact they are still in use",
    ),
}
"""Keys Django reads only from inside a ``DATABASES`` alias.

Deliberately limited to the five that change behaviour. Nobody writes ``ENGINE``
at module level believing Django will read it, but people write ``CONN_MAX_AGE``
there constantly, because every article showing the fix shows the line without
the surrounding dictionary.
"""


def references(view: SettingsView, name: str) -> bool:
    """Whether ``DATABASES`` in this module mentions ``name``.

    ``CONN_MAX_AGE = int(os.getenv(...))`` followed by ``"CONN_MAX_AGE":
    CONN_MAX_AGE`` inside the alias is a perfectly good way to write this, and
    it is indistinguishable from the mistake until you look for the reference.
    """
    return any(
        isinstance(child, ast.Name) and child.id == name
        for definition in view.get("DATABASES").definitions
        for child in ast.walk(definition.node)
    )


@register
class DatabaseKeyAtModuleLevel(SettingsRule):
    """A per-alias database key written where Django will never read it.

    This is the same class of defect as DJS-012 and DJS-014 and the most
    valuable thing this analyzer does: a line that looks like configuration,
    reads like configuration, survives every review, and has no effect
    whatsoever. Nothing warns. ``manage.py check --deploy`` says nothing,
    because there is no such setting to complain about.
    """

    setting = ""
    ceiling = Confidence.CERTAIN
    """Django's ConnectionHandler reads these out of the alias mapping and
    nowhere else. That is not an inference about a deployment, it is what the
    framework does."""

    caveats = (
        "a project is free to read a name like this out of its own settings for its "
        "own purposes, which would not be visible here",
    )

    def selects(self, name: str) -> bool:
        return name in PER_ALIAS_KEYS

    def inspect(self, ctx: ProjectContext, group: SettingGroup) -> Iterator[Finding]:
        resolved = group.setting
        if not resolved.is_explicit:
            return
        if any(references(view, resolved.name) for view in self.views.values()):
            return

        severity, belief = PER_ALIAS_KEYS[resolved.name]
        where = group.module.dotted or ctx.rel(group.module.path)
        yield self.report(
            ctx,
            group,
            severity=severity,
            message=(
                f"{resolved.name} is assigned at module level in {where}"
                f"{group.describe_reach()}, but Django only ever reads it from inside "
                f"a DATABASES alias, so the line does nothing -- and someone reading "
                f"it will believe {belief}"
            ),
            evidence=(
                Evidence(
                    kind=EvidenceKind.CONFIG,
                    content=(
                        f"{resolved.name} = {resolved.value.describe()}\n"
                        f"read by Django from: DATABASES[alias][{resolved.name!r}]"
                    ),
                    source="djaudit settings resolver",
                ),
            ),
        )

    meta = RuleMeta(
        id="DJS-023",
        title="a per-alias database key is set where Django cannot see it",
        family=Family.DJS,
        severity=Severity.MEDIUM,
        confidence=Confidence.CERTAIN,
        tier=Tier.STATIC,
        rationale=(
            "ATOMIC_REQUESTS, AUTOCOMMIT, CONN_MAX_AGE, CONN_HEALTH_CHECKS and "
            "DISABLE_SERVER_SIDE_CURSORS are not Django settings. They are keys inside "
            "a DATABASES alias: ConnectionHandler.configure_settings() fills their "
            "defaults in per connection, and django.core.handlers.base reads "
            "settings_dict['ATOMIC_REQUESTS'] rather than settings.ATOMIC_REQUESTS. "
            "None of them appear in global_settings.py at all, so assigning one at "
            "module level is not overriding anything -- it is creating a new setting "
            "nothing reads. Nothing warns about it. There is no system check for a "
            "setting that does not exist, `manage.py check --deploy` is silent, and "
            "the application behaves exactly as it did before the line was added, "
            "which is the problem: with ATOMIC_REQUESTS the author now believes "
            "failed views roll back, and they do not."
        ),
        remediation=(
            "Move the key inside the alias it is meant to configure: "
            "DATABASES['default']['ATOMIC_REQUESTS'] = True, or write it into the "
            "dictionary literal. If a module-level constant is wanted for readability, "
            "keep it and reference it from inside the alias -- CONN_MAX_AGE at module "
            "level is only a defect when nothing puts it into DATABASES. Note that "
            "ATOMIC_REQUESTS applies per alias, so a project with a replica has to "
            "decide for each one, and that Django will refuse to start if it is "
            "combined with an async view."
        ),
        references=(
            _DATABASES_DOCS,
            "https://docs.djangoproject.com/en/stable/topics/db/transactions/#tying-transactions-to-http-requests",
            "https://docs.djangoproject.com/en/stable/ref/settings/#std-setting-DATABASE-ATOMIC_REQUESTS",
        ),
        limitations=(
            "Django's ConnectionHandler reads these keys out of the alias mapping and nowhere "
            "else, so the claim is about the framework rather than a deployment. The limit is "
            "the list of key names: one this rule does not know is not reported.",
        ),
    )

"""Which database vendor a settings module can actually reach.

Every `DJX` rule starts from the same question -- *can this project run on
more than one database?* -- and the answer decides whether the rest of the
family is relevant at all. A project that only ever runs Postgres may use
`distinct('field')` freely; the identical line in a project a developer runs
on SQLite is a bug that only appears in production.

Two things about that question turned out to be different from what the plan
assumed, and both shaped this module.

**Divergence lives inside one settings module, not across two.** The plan
described the shape as SQLite in a development settings module and Postgres in
a production one, to be found by comparing `SettingsRole`. No project in the
benchmark corpus is written that way. Healthchecks -- named in the plan as the
ideal target because it genuinely supports both -- assigns `DATABASES` three
times in a single module: SQLite unconditionally, then Postgres inside
`if os.getenv("DB") == "postgres":`, then MySQL inside another `if`. One
module, one role, three engines. pretix computes the engine by concatenating a
config value onto `django.db.backends.`. NetBox sets it with a `.update()` on
a dict imported from elsewhere. The cross-module shape the plan described is
real in the wild, but it is not the common one, so this module gathers choices
from everywhere and compares them globally rather than pairing modules by role.

**Vendor is a table lookup, not a substring test.** `"sqlite" in engine` reads
plausibly and is wrong: Django's GIS backend for SQLite is `spatialite`, which
contains no `sqlite` substring, and its backend for Postgres is `postgis`,
which contains no `postgres` substring. Wrappers move the vendor name out of
the leaf entirely -- NetBox ships `django_prometheus.db.backends.postgresql`.
:data:`_LEAF` and :data:`_PATHS` are the specification; anything they do not
recognise is :attr:`Vendor.UNKNOWN`, which is a real answer that rules must
handle rather than a lookup failure to paper over.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from enum import StrEnum

from djaudit.context import ProjectContext, SettingsModule
from djaudit.settings import (
    Definition,
    Entry,
    ResolvedSetting,
    SettingsView,
    entries,
    literal_text,
)


class Vendor(StrEnum):
    """The database a `DATABASES` entry actually talks to.

    Named for the server rather than the Django backend because that is the
    level portability questions live at: `postgis` and `postgresql` differ in
    what they can store and not at all in how they collate text, quote
    identifiers or enforce `max_length`.
    """

    SQLITE = "sqlite"
    POSTGRESQL = "postgresql"
    MYSQL = "mysql"
    ORACLE = "oracle"
    UNKNOWN = "unknown"
    """The engine string was unreadable, or names a backend not in the table.

    Distinct from "no engine": a rule that treats this as "not SQLite" will
    quietly stop firing on the projects hardest to analyse.
    """

    @property
    def known(self) -> bool:
        return self is not Vendor.UNKNOWN


_LEAF: dict[str, Vendor] = {
    "sqlite3": Vendor.SQLITE,
    "sqlite": Vendor.SQLITE,
    "spatialite": Vendor.SQLITE,
    "postgresql": Vendor.POSTGRESQL,
    "postgres": Vendor.POSTGRESQL,
    "postgresql_psycopg2": Vendor.POSTGRESQL,
    "postgis": Vendor.POSTGRESQL,
    "mysql": Vendor.MYSQL,
    "mariadb": Vendor.MYSQL,
    "oracle": Vendor.ORACLE,
}
"""Vendor by the last dotted segment, after a `_backend` suffix is stripped.

Covers Django's own backends, its GIS backends, and every third-party wrapper
that keeps the vendor in the leaf -- `django_prometheus.db.backends.postgresql`
and `django_tenants.postgresql_backend` both land here without an entry of
their own.
"""

_PATHS: dict[str, Vendor] = {
    "psqlextra.backend": Vendor.POSTGRESQL,
    "mysql.connector.django": Vendor.MYSQL,
}
"""Backends whose leaf segment names no vendor, matched on the whole path.

Deliberately short. Guessing from a package name that merely looks
database-shaped is how `spatialite` would have been missed in the other
direction, so a backend earns a row here only once someone has confirmed which
server it speaks to.
"""


def classify(engine: str | None) -> Vendor:
    """The vendor an `ENGINE` string names."""
    if engine is None:
        return Vendor.UNKNOWN
    path = engine.strip()
    if path in _PATHS:
        return _PATHS[path]
    leaf = path.rpartition(".")[2].removesuffix("_backend")
    return _LEAF.get(leaf, Vendor.UNKNOWN)


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
    def vendor(self) -> Vendor:
        return classify(self.engine)

    @property
    def is_sqlite(self) -> bool:
        return self.vendor is Vendor.SQLITE

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


@dataclass(frozen=True)
class EngineChoice:
    """One database this project could end up connected to.

    One assignment to one alias in one settings module. A project has as many
    of these as it has branches, which is the point: the divergence rules need
    the set, and a resolver that collapses `DATABASES` to a single value hands
    back whichever branch was written last.
    """

    module: SettingsModule
    alias: str
    vendor: Vendor
    engine: str | None
    """The `ENGINE` string as written, or `None` when it was not readable.

    Unreadable and absent are both `None` here because they have the same
    consequence -- pretix builds the string by concatenation, NetBox sets it
    through a `.update()` on an imported dict, and in neither case can a rule
    say what runs. :attr:`vendor` is `UNKNOWN` for both.
    """

    conditional: bool
    """Whether the assignment sits inside an `if` or `try`.

    This is what separates the two halves of a divergence. An unconditional
    assignment is what a developer gets having set nothing, so it is the
    development database whether or not anyone calls it that; a conditional one
    is a deployment opting in.
    """

    definition: Definition
    node: ast.expr | None

    @property
    def default(self) -> bool:
        """Whether this is what runs when no environment variable is set."""
        return not self.conditional

    @property
    def line(self) -> int:
        return self.definition.line

    def describe(self) -> str:
        """A phrase naming the engine and where it was written."""
        where = self.module.dotted or self.module.path.name
        engine = self.engine if self.engine is not None else "an engine we cannot read"
        return f"{engine} at {where}:{self.line}"


def config_choice(module: SettingsModule, alias: str, config: DatabaseConfig) -> EngineChoice:
    """Lift one alias configuration into a vendor decision."""
    engine = config.engine
    return EngineChoice(
        module=module,
        alias=alias,
        vendor=classify(engine),
        engine=engine,
        conditional=config.conditional,
        definition=config.definition,
        node=config.at(),
    )


def module_engines(module: SettingsModule, view: SettingsView) -> tuple[EngineChoice, ...]:
    """Every database one settings module could reach, in source order.

    Includes conditional branches and every alias, because a project with a
    Postgres `default` and a SQLite `replica` has a portability problem that
    reading only `default` would miss.
    """
    resolved = view.get("DATABASES")
    choices: list[EngineChoice] = []
    for alias, configs in database_configs(view, resolved).items():
        choices.extend(config_choice(module, alias, config) for config in configs)
    return tuple(sorted(choices, key=lambda choice: (choice.line, choice.alias)))


def project_engines(
    ctx: ProjectContext, views: dict[str, SettingsView]
) -> tuple[EngineChoice, ...]:
    """Every database the whole project could reach.

    Flat rather than keyed by module because divergence is a property of the
    project: two settings modules each naming one engine diverge exactly as
    much as one module naming two, and a caller handed a mapping would have to
    flatten it to notice.
    """
    choices: list[EngineChoice] = []
    for module in ctx.settings_modules:
        view = views.get(module.dotted)
        if view is None:
            continue
        choices.extend(module_engines(module, view))
    return tuple(choices)

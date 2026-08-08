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


def module_name(module: SettingsModule) -> str:
    """A settings module's dotted name, or its filename when it has none.

    A module that is its tree's root package resolves to an empty dotted name,
    and naming a finding after the empty string names nothing.
    """
    return module.dotted or module.path.name


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

    guard: str | None = None
    """The `if` test that selects this branch, as written.

    A finding saying "line 221 is conditional" tells a reader to go and look;
    one saying `os.getenv('DB') == 'postgres'` tells them which lever moves the
    project between the two databases, which is the actionable half. `None`
    when the assignment is unconditional or the guard could not be recovered.
    """

    @property
    def default(self) -> bool:
        """Whether this is what runs when no environment variable is set."""
        return not self.conditional

    @property
    def line(self) -> int:
        return self.definition.line

    def describe(self) -> str:
        """A phrase naming the engine and where it was written."""
        where = module_name(self.module)
        engine = self.engine if self.engine is not None else "an engine we cannot read"
        return f"{engine} at {where}:{self.line}"

    def selected_by(self) -> str:
        """How a deployment ends up on this database."""
        if self.guard is not None:
            return f"when {self.guard}"
        if self.conditional:
            return "on a branch we could not name"
        return "with nothing set"


def guards(tree: ast.Module | None) -> dict[int, str]:
    """Every statement's enclosing `if` tests, keyed by node identity.

    The resolver knows an assignment is conditional but discards the test that
    guards it, and the test is the useful part -- it names the environment
    variable a reader has to set to move between the two databases. Recovering
    it here rather than threading it through the resolver keeps a change to
    every settings rule out of a question only this family asks.

    Nested guards are joined because both must hold, and an `else` is recorded
    negated for the same reason: a branch is reached on the whole chain, not on
    the innermost test.
    """
    found: dict[int, str] = {}
    if tree is None:
        return found

    def walk(body: list[ast.stmt], chain: tuple[str, ...]) -> None:
        for stmt in body:
            if chain:
                found[id(stmt)] = " and ".join(chain)
            if isinstance(stmt, ast.If):
                test = ast.unparse(stmt.test)
                walk(stmt.body, (*chain, test))
                walk(stmt.orelse, (*chain, f"not ({test})"))

    walk(tree.body, ())
    return found


def config_choice(
    module: SettingsModule,
    alias: str,
    config: DatabaseConfig,
    found: dict[int, str] | None = None,
) -> EngineChoice:
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
        guard=(found or {}).get(id(config.definition.node)),
    )


def module_engines(
    module: SettingsModule, view: SettingsView, tree: ast.Module | None = None
) -> tuple[EngineChoice, ...]:
    """Every database one settings module could reach, in source order.

    Includes conditional branches and every alias, because a project with a
    Postgres `default` and a SQLite `replica` has a portability problem that
    reading only `default` would miss.
    """
    resolved = view.get("DATABASES")
    found = guards(tree)
    choices: list[EngineChoice] = []
    for alias, configs in database_configs(view, resolved).items():
        choices.extend(config_choice(module, alias, config, found) for config in configs)
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
        choices.extend(module_engines(module, view, ctx.parse(module.path)))
    return tuple(choices)


class Portability(StrEnum):
    """What the set of reachable engines says about a project.

    Four states rather than a boolean because "we could not tell" has to be
    distinguishable from "we checked and it is fine". A `DJX` rule that treats
    :attr:`UNCERTAIN` or :attr:`UNREADABLE` as :attr:`SINGLE` stops firing on
    the projects whose settings are hardest to read, which are not the projects
    least likely to have the bug.
    """

    SINGLE = "single"
    """Every readable engine names the same vendor, and all of them were read."""

    DIVERGENT = "divergent"
    """One alias can be two different databases, so the family below applies."""

    UNCERTAIN = "uncertain"
    """An engine could not be read, so a second vendor cannot be ruled out."""

    UNREADABLE = "unreadable"
    """No engine was found at all. Not evidence of anything."""


@dataclass(frozen=True)
class Divergence:
    """Whether this project can run on more than one database.

    Computed per alias and then aggregated, because an alias is one connection
    and it is one connection changing vendor that makes SQL portability a
    question. A Postgres `default` beside a SQLite `replica` is a different
    defect, and folding the two together would report each as the other.
    """

    verdict: Portability
    choices: tuple[EngineChoice, ...]
    alias: str | None = None
    """The alias that diverges, when one does."""

    @property
    def diverges(self) -> bool:
        return self.verdict is Portability.DIVERGENT

    @property
    def vendors(self) -> frozenset[Vendor]:
        """Every vendor this project could reach, ignoring the unreadable ones."""
        return frozenset(c.vendor for c in self.choices if c.vendor.known)

    def reaches(self, vendor: Vendor) -> bool:
        """Whether `vendor` is evidenced as a database this project uses.

        Project-wide on purpose: a query cannot say statically which alias it
        will run against, so a rule about SQL that only one backend accepts has
        to ask the question of the whole project.

        Evidence, not possibility. Answering "maybe" here would make every
        `DJX` rule fire on NetBox and pretix -- whose engines are genuinely
        unreadable -- and the family's whole claim is that a divergence is a
        thing you can point at. :meth:`could_reach` is the other question.
        """
        return vendor in self.vendors

    def could_reach(self, vendor: Vendor) -> bool:
        """Whether `vendor` cannot be ruled out.

        True wherever :meth:`reaches` is, plus the cases where we failed to
        read an engine. A rule using this instead of :meth:`reaches` is
        choosing recall over precision and owes its finding a `tentative`
        confidence.
        """
        return self.reaches(vendor) or not self.conclusive

    @property
    def conclusive(self) -> bool:
        """Whether every engine was readable, so the vendor set is complete."""
        return self.verdict not in _NOT_EXCLUDED

    @property
    def default(self) -> EngineChoice | None:
        """What runs when nothing is set -- the developer's database.

        When the engines are split across modules they are all unconditional,
        so conditionality alone cannot say which one a developer gets. The
        module's role can: a module that does not reach production is the
        development one by definition, and it is preferred here. This is the
        one place the role comparison the plan described is the right tool.
        """
        plain = [c for c in self.relevant if c.default]
        development = [c for c in plain if not c.module.role.reaches_production]
        return next(iter(development or plain), None)

    @property
    def alternatives(self) -> tuple[EngineChoice, ...]:
        """The other databases, whose vendor differs from the default's.

        Not restricted to conditional assignments. Two settings modules each
        assigning a different engine unconditionally are a divergence with no
        `if` anywhere in it, and requiring one here made this return nothing
        for exactly the shape the plan set out to catch.
        """
        default = self.default
        if default is None:
            return ()
        return tuple(
            c for c in self.relevant if c is not default and c.vendor is not default.vendor
        )

    @property
    def relevant(self) -> tuple[EngineChoice, ...]:
        """The choices for the diverging alias, or all of them."""
        if self.alias is None:
            return self.choices
        return tuple(c for c in self.choices if c.alias == self.alias)


_NOT_EXCLUDED = frozenset({Portability.UNCERTAIN, Portability.UNREADABLE})
"""Verdicts under which no vendor can be ruled out.

Named rather than inlined so the claim is stated once: an engine we could not
read might be any of them, and a project with no readable engine at all might
be any of them too.
"""


def divergence(choices: tuple[EngineChoice, ...]) -> Divergence:
    """Decide whether more than one database is reachable."""
    if not choices:
        return Divergence(Portability.UNREADABLE, choices)

    by_alias: dict[str, set[Vendor]] = {}
    for choice in choices:
        if choice.vendor.known:
            by_alias.setdefault(choice.alias, set()).add(choice.vendor)

    diverging = sorted(alias for alias, vendors in by_alias.items() if len(vendors) > 1)
    if diverging:
        return Divergence(Portability.DIVERGENT, choices, diverging[0])
    if any(not choice.vendor.known for choice in choices):
        return Divergence(Portability.UNCERTAIN, choices)
    return Divergence(Portability.SINGLE, choices)


def portability(ctx: ProjectContext, views: dict[str, SettingsView]) -> Divergence:
    """The whole question, from a context: what can this project connect to?"""
    return divergence(project_engines(ctx, views))

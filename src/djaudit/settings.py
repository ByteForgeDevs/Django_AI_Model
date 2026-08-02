"""Resolving what a Django setting is actually worth, and where it came from.

A rule that reads one assignment in one file gets split settings wrong. Real
projects put ``DEBUG = True`` in ``base.py`` and ``DEBUG = False`` in
``production.py``, and the answer to "is DEBUG on" depends on which module you
are asking about and what it inherits.

This module answers that question once, with provenance, so no rule has to
reimplement it -- and so a finding can say *which* assignment wins and which
were overridden.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from djaudit.astutils import StarImport, star_imports
from djaudit.context import ProjectContext, SettingsModule
from djaudit.discovery import dotted_path, resolve_dotted
from djaudit.evaluator import (
    Evaluator,
    Scope,
    collect_env_objects,
    collect_functions,
    collect_imports,
)
from djaudit.models import Confidence
from djaudit.values import Value


class Origin(StrEnum):
    """Where a setting's effective value came from.

    The distinction is not cosmetic: a project that never mentions
    ``SECURE_SSL_REDIRECT`` and one that explicitly sets it to ``False`` have
    the same value and very different intent, and a good finding says which.
    """

    EXPLICIT = "explicit"
    """Assigned somewhere in the settings chain."""

    DJANGO_DEFAULT = "django_default"
    """Never assigned; Django's documented default applies."""

    UNRESOLVED = "unresolved"
    """Assigned, but the value could not be determined statically."""

    ABSENT = "absent"
    """Never assigned and we hold no default for it."""


@dataclass(frozen=True, slots=True)
class Definition:
    """One assignment of one setting in one module."""

    name: str
    value: Value
    module: Path
    dotted: str
    node: ast.stmt
    conditional: bool = False
    """Nested in ``if``/``try``, so it may not execute."""

    @property
    def line(self) -> int:
        return self.node.lineno

    def describe(self) -> str:
        where = f"{self.dotted or self.module.name}:{self.line}"
        suffix = " (conditional)" if self.conditional else ""
        return f"{where} -> {self.value.describe()}{suffix}"


@dataclass(frozen=True, slots=True)
class ResolvedSetting:
    """A setting's effective value plus every assignment that contributed."""

    name: str
    value: Value
    origin: Origin
    definitions: tuple[Definition, ...] = ()
    """In resolution order. The last one wins."""

    @property
    def definition(self) -> Definition | None:
        """The assignment that determines the value, if there is one."""
        return self.definitions[-1] if self.definitions else None

    @property
    def overridden(self) -> tuple[Definition, ...]:
        """Assignments a later one replaced, oldest first."""
        return self.definitions[:-1]

    @property
    def is_explicit(self) -> bool:
        return self.origin is Origin.EXPLICIT

    @property
    def is_default(self) -> bool:
        return self.origin is Origin.DJANGO_DEFAULT

    @property
    def conditional(self) -> bool:
        """Whether the winning assignment might not execute."""
        winner = self.definition
        return winner.conditional if winner else False

    def is_always(self, expected: object) -> bool:
        return self.value.is_always(expected)

    def could_be(self, expected: object) -> bool:
        return self.value.could_be(expected)

    def describe(self) -> str:
        if self.origin is Origin.DJANGO_DEFAULT:
            return f"{self.name} = {self.value.describe()} (Django default, never set)"
        if self.origin is Origin.ABSENT:
            return f"{self.name} is not set"
        return f"{self.name} = {self.value.describe()}"

    def provenance(self) -> str:
        """A one-line override chain, for use as finding evidence."""
        if not self.definitions:
            return self.describe()
        return " | ".join(definition.describe() for definition in self.definitions)


@dataclass(frozen=True, slots=True)
class _Operation:
    """One module-level statement that changes a setting, in source order."""

    name: str
    node: ast.stmt
    conditional: bool
    value: ast.expr | None = None
    augmented: bool = False
    method: str = ""
    args: tuple[ast.expr, ...] = ()


def _operations(tree: ast.Module, scope: Scope) -> Iterator[_Operation]:
    """Assignments and in-place mutations of module-level names, in order.

    Branches are evaluated as the walk proceeds rather than all being taken.
    NetBox guards its debug toolbar with ``if DEBUG:``, so a walk that enters
    every branch reports the debug toolbar as installed in production -- a
    false positive on the exact repository we use to measure false positives.
    """

    def visit(body: list[ast.stmt], conditional: bool) -> Iterator[_Operation]:
        for stmt in body:
            if isinstance(stmt, ast.Assign):
                for target in stmt.targets:
                    if isinstance(target, ast.Name):
                        yield _Operation(target.id, stmt, conditional, value=stmt.value)
            elif isinstance(stmt, ast.AnnAssign):
                if isinstance(stmt.target, ast.Name) and stmt.value is not None:
                    yield _Operation(stmt.target.id, stmt, conditional, value=stmt.value)
            elif isinstance(stmt, ast.AugAssign):
                if isinstance(stmt.target, ast.Name):
                    yield _Operation(
                        stmt.target.id, stmt, conditional, value=stmt.value, augmented=True
                    )
            elif isinstance(stmt, ast.Expr):
                mutation = _mutation(stmt, conditional)
                if mutation is not None:
                    yield mutation
            elif isinstance(stmt, ast.If):
                yield from _visit_if(stmt, conditional, visit, scope)
            elif isinstance(stmt, ast.Try):
                # An exception may fire at any point, so nothing here is certain.
                yield from visit(stmt.body, True)
                for handler in stmt.handlers:
                    yield from visit(handler.body, True)
                yield from visit(stmt.orelse, True)
                yield from visit(stmt.finalbody, conditional)
            elif isinstance(stmt, ast.With):
                yield from visit(stmt.body, conditional)

    yield from visit(tree.body, False)


def _visit_if(
    stmt: ast.If,
    conditional: bool,
    visit: Callable[[list[ast.stmt], bool], Iterator[_Operation]],
    scope: Scope,
) -> Iterator[_Operation]:
    """Take the branch that runs, or both when we cannot prove which does."""
    test = Evaluator(scope).evaluate(stmt.test)

    # Prune only when the guard can never go the other way. Resolving a test to
    # False for *our* environment says nothing about the deployment's, so an
    # environment-dependent guard keeps both branches even though we have a
    # value for it -- discarding one here would be permanent, and it is the
    # branch a misconfigured deployment takes.
    if test.is_literal and not test.env_dependent:
        yield from visit(stmt.body if test.literal else stmt.orelse, conditional)
        return

    yield from visit(stmt.body, True)
    yield from visit(stmt.orelse, True)


def _mutation(stmt: ast.Expr, conditional: bool) -> _Operation | None:
    call = stmt.value
    if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
        return None
    receiver = call.func.value
    if not isinstance(receiver, ast.Name) or call.func.attr not in _LIST_MUTATIONS:
        return None
    return _Operation(
        receiver.id,
        stmt,
        conditional,
        method=call.func.attr,
        args=tuple(call.args),
    )


_LIST_MUTATIONS = frozenset({"append", "insert", "extend", "remove"})


@dataclass(frozen=True, slots=True)
class SettingsView:
    """Every setting visible from one settings module, with inheritance applied."""

    module: SettingsModule
    chain: tuple[str, ...]
    """Modules in resolution order, ending with ``module`` itself."""

    settings: dict[str, ResolvedSetting]
    django_version: str | None = None

    def get(self, name: str) -> ResolvedSetting:
        """The setting's effective value, falling back to Django's default."""
        found = self.settings.get(name)
        if found is not None:
            return found
        default = django_default(name, self.django_version)
        if default is not None:
            return ResolvedSetting(name, default, Origin.DJANGO_DEFAULT)
        return ResolvedSetting(name, Value.unknown("setting is not assigned"), Origin.ABSENT)

    def __contains__(self, name: str) -> bool:
        return name in self.settings


def resolve_settings(ctx: ProjectContext, module: SettingsModule) -> SettingsView:
    """Resolve one settings module, following ``import *`` inheritance.

    A star import copies names into the importing module's namespace, so the
    chain really does share one namespace at runtime. Threading a single scope
    through it in source order reproduces that, which is what makes
    ``production.py`` overriding ``base.py`` come out right without any
    special-case override logic.
    """
    chain = _inheritance_chain(ctx, module)
    scope = Scope()
    definitions: dict[str, list[Definition]] = {}

    for path in chain:
        tree = ctx.parse(path)
        if tree is None:
            continue

        # A star import copies the imported module's imports, helpers and env
        # objects too, so merging them is faithful rather than approximate.
        scope.imports.update(collect_imports(tree))
        scope.functions.update(collect_functions(tree))
        scope.env_objects.update(collect_env_objects(tree, scope.imports))

        dotted = dotted_path(ctx.root, path)
        for operation in _operations(tree, scope):
            value = _apply(operation, scope)
            if value is None:
                continue
            if operation.conditional:
                # The assignment may not run, so the earlier value survives as
                # an alternative rather than being replaced by this one.
                value = _either(scope.names.get(operation.name), value)
            scope.names[operation.name] = value
            if operation.name.isupper():
                definitions.setdefault(operation.name, []).append(
                    Definition(
                        name=operation.name,
                        value=value,
                        module=path,
                        dotted=dotted,
                        node=operation.node,
                        conditional=operation.conditional,
                    )
                )

    return SettingsView(
        module=module,
        chain=tuple(dotted_path(ctx.root, path) for path in chain),
        settings={name: _resolved(name, found) for name, found in definitions.items()},
        django_version=ctx.django_version,
    )


def resolve_all(ctx: ProjectContext) -> dict[str, SettingsView]:
    """Resolve every discovered settings module, keyed by dotted path."""
    return {module.dotted: resolve_settings(ctx, module) for module in ctx.settings_modules}


def _resolved(name: str, definitions: list[Definition]) -> ResolvedSetting:
    winner = definitions[-1]
    origin = Origin.EXPLICIT if not winner.value.is_unknown else Origin.UNRESOLVED
    return ResolvedSetting(
        name=name,
        value=winner.value,
        origin=origin,
        definitions=tuple(definitions),
    )


def _either(previous: Value | None, value: Value) -> Value:
    """Combine a value with the one a conditional assignment might not replace."""
    if previous is None:
        return value
    return Value.conditional([previous, value])


def _apply(operation: _Operation, scope: Scope) -> Value | None:
    """Compute the value a single operation leaves behind."""
    evaluator = Evaluator(scope)

    if operation.method:
        return _mutate(operation, scope, evaluator)

    if operation.value is None:
        return None
    value = evaluator.evaluate(operation.value)

    if not operation.augmented:
        return value

    # MIDDLEWARE += [...] -- the previous value matters.
    current = scope.names.get(operation.name)
    if current is None or not current.is_literal or not value.is_literal:
        return Value.unknown(f"unresolvable augmented assignment to {operation.name}")
    try:
        combined = current.literal + value.literal
    except TypeError:
        return Value.unknown(f"incompatible augmented assignment to {operation.name}")
    return Value.of(combined, env_dependent=current.env_dependent or value.env_dependent)


def _mutate(operation: _Operation, scope: Scope, evaluator: Evaluator) -> Value | None:
    current = scope.names.get(operation.name)
    if current is None:
        return None
    if not current.is_literal or not isinstance(current.literal, list):
        return Value.unknown(f"mutation of an unresolved {operation.name}")

    arguments = [evaluator.evaluate(argument) for argument in operation.args]
    if not all(argument.is_literal for argument in arguments):
        return Value.unknown(f"unresolvable argument to {operation.name}.{operation.method}()")

    updated = list(current.literal)
    try:
        getattr(updated, operation.method)(*[argument.literal for argument in arguments])
    except (TypeError, ValueError, IndexError):
        return Value.unknown(f"{operation.name}.{operation.method}() failed")

    tainted = current.env_dependent or any(argument.env_dependent for argument in arguments)
    return Value.of(updated, env_dependent=tainted)


def _inheritance_chain(ctx: ProjectContext, module: SettingsModule) -> list[Path]:
    """Star-imported modules first, in source order, then the module itself."""
    ordered: list[Path] = []
    seen: set[Path] = set()

    def walk(path: Path) -> None:
        if path in seen:
            # A cycle is not valid Python at runtime either, but a half-written
            # settings package should not hang the analyser.
            return
        seen.add(path)
        tree = ctx.parse(path)
        if tree is not None:
            for star in star_imports(tree):
                target = _resolve_star(ctx, path, star)
                if target is not None:
                    walk(target)
        ordered.append(path)

    walk(module.path)
    return ordered


def _resolve_star(ctx: ProjectContext, importer: Path, star: StarImport) -> Path | None:
    if not star.is_relative:
        return resolve_dotted(ctx.root, star.module)

    # `from .base import *` is relative to the importing module's package;
    # each extra dot climbs one more level.
    package = importer.parent
    for _ in range(star.level - 1):
        package = package.parent
    if not star.module:
        return None
    relative = Path(*star.module.split("."))
    for candidate in (
        package / relative.with_suffix(".py"),
        package / relative / "__init__.py",
    ):
        if candidate.is_file():
            return candidate
    return None


# Django's own defaults, read out of django.conf.global_settings rather than
# transcribed, and checked against it by tests/test_django_defaults.py.
#
# "Never set" and "explicitly set to the same value" are different facts about
# a project: the first is usually an oversight and the second is a decision, so
# the finding text and the remediation differ.
DJANGO_DEFAULTS: dict[str, Any] = {
    "ALLOWED_HOSTS": [],
    "AUTH_PASSWORD_VALIDATORS": [],
    "CSRF_COOKIE_HTTPONLY": False,
    "CSRF_COOKIE_SAMESITE": "Lax",
    "CSRF_COOKIE_SECURE": False,
    "CSRF_TRUSTED_ORIGINS": [],
    "DEBUG": False,
    "DEBUG_PROPAGATE_EXCEPTIONS": False,
    "EMAIL_USE_TLS": False,
    "INSTALLED_APPS": [],
    "LOGGING": {},
    "MIDDLEWARE": [],
    "PASSWORD_HASHERS": [
        "django.contrib.auth.hashers.PBKDF2PasswordHasher",
        "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
        "django.contrib.auth.hashers.Argon2PasswordHasher",
        "django.contrib.auth.hashers.BCryptSHA256PasswordHasher",
        "django.contrib.auth.hashers.ScryptPasswordHasher",
    ],
    "SECRET_KEY": "",
    "SECURE_CONTENT_TYPE_NOSNIFF": True,
    "SECURE_CROSS_ORIGIN_OPENER_POLICY": "same-origin",
    "SECURE_HSTS_INCLUDE_SUBDOMAINS": False,
    "SECURE_HSTS_PRELOAD": False,
    "SECURE_HSTS_SECONDS": 0,
    "SECURE_PROXY_SSL_HEADER": None,
    "SECURE_REFERRER_POLICY": "same-origin",
    "SECURE_SSL_REDIRECT": False,
    "SESSION_COOKIE_HTTPONLY": True,
    "SESSION_COOKIE_SAMESITE": "Lax",
    "SESSION_COOKIE_SECURE": False,
    "USE_X_FORWARDED_HOST": False,
}

# Defaults that Django changed between supported releases. Reporting the wrong
# one is exactly the version drift the plan lists as a risk, so an unknown
# Django version yields no default at all rather than a guess.
DEFAULTS_BY_VERSION: dict[tuple[int, int], dict[str, Any]] = {
    (5, 2): {"DEFAULT_AUTO_FIELD": "django.db.models.AutoField"},
    (6, 0): {"DEFAULT_AUTO_FIELD": "django.db.models.BigAutoField"},
}

# Settings that do not exist before a given release. A rule must not report a
# 6.0-only setting as missing from a 5.2 project.
INTRODUCED_IN: dict[str, tuple[int, int]] = {
    "SECURE_CSP": (6, 0),
    "SECURE_CSP_REPORT_ONLY": (6, 0),
    "TASKS": (6, 0),
    "URLIZE_ASSUME_HTTPS": (6, 0),
}

# Per-connection options, which live inside DATABASES[alias] rather than at
# module level, so they are not part of the flat table.
DATABASE_DEFAULTS: dict[str, Any] = {
    "ATOMIC_REQUESTS": False,
    "CONN_HEALTH_CHECKS": False,
    "CONN_MAX_AGE": 0,
    "AUTOCOMMIT": True,
}


def parse_version(version: str | None) -> tuple[int, int] | None:
    """``"5.2.1"`` -> ``(5, 2)``. Anything unparseable is None."""
    if not version:
        return None
    parts = version.split(".")
    try:
        return int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    except ValueError:
        return None


def django_default(name: str, version: str | None = None) -> Value | None:
    """Django's default for ``name``, or None when we do not know one.

    None is returned rather than a guess in three cases: the setting is not in
    our table, its default differs across supported releases and we could not
    detect which one is in use, or it postdates the detected release.
    """
    release = parse_version(version)

    introduced = INTRODUCED_IN.get(name)
    if introduced is not None and (release is None or release < introduced):
        return None

    if name in DJANGO_DEFAULTS:
        return Value.of(DJANGO_DEFAULTS[name])

    if release is not None:
        versioned = DEFAULTS_BY_VERSION.get(release)
        if versioned is not None and name in versioned:
            return Value.of(versioned[name])

    return None


# -- confidence policy -------------------------------------------------------
#
# One mapping from resolution quality to Confidence, applied by every rule.
# Written down once because the alternative is each rule inventing its own,
# which makes the confidence field meaningless across a report -- and it is the
# field CI gates on.

_CONFIDENCE_ORDER: tuple[Confidence, ...] = (
    Confidence.CERTAIN,
    Confidence.FIRM,
    Confidence.TENTATIVE,
)


def lower_confidence(confidence: Confidence, steps: int = 1) -> Confidence:
    """Reduce ``confidence`` by ``steps``, stopping at TENTATIVE."""
    index = _CONFIDENCE_ORDER.index(confidence) + max(steps, 0)
    return _CONFIDENCE_ORDER[min(index, len(_CONFIDENCE_ORDER) - 1)]


@dataclass(frozen=True, slots=True)
class Assessment:
    """How far a finding about a setting can be trusted, and why."""

    confidence: Confidence
    caveats: tuple[str, ...] = ()

    def note(self) -> str:
        """The caveats as a parenthesised clause, or empty."""
        return f" ({'; '.join(self.caveats)})" if self.caveats else ""


def assess(resolved: ResolvedSetting, ceiling: Confidence = Confidence.CERTAIN) -> Assessment:
    """Grade a finding built on ``resolved``.

    ``ceiling`` is the best a rule could claim if resolution were perfect; a
    rule that infers rather than observes passes something lower.
    """
    caveats: list[str] = []
    steps = 0

    if resolved.origin is Origin.UNRESOLVED:
        # Rules should not usually report on these at all.
        return Assessment(Confidence.TENTATIVE, ("value could not be determined statically",))

    if resolved.value.is_conditional:
        steps += 2
        caveats.append("the setting takes different values on different paths")

    if resolved.conditional:
        steps += 1
        caveats.append("the assignment is inside a conditional block, so it may not execute")

    if resolved.value.env_dependent:
        steps += 1
        caveats.append("the value comes from the environment, so a deployment may override it")

    if resolved.origin is Origin.DJANGO_DEFAULT:
        steps += 1
        caveats.append("the setting is never assigned, so Django's default applies")

    return Assessment(lower_confidence(ceiling, steps), tuple(caveats))

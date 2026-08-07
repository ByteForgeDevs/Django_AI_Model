"""Lexical scopes and name bindings within a module.

The substrate for every `DJP` and `DJI` rule. Asking "what is this name?" is the
first question all of them need answered, and answering it with "the last
assignment to that name anywhere in the file" is how a static analyser starts
producing confident nonsense.

Python's scoping has four rules that a naive implementation gets wrong, and each
one is a false positive generator:

**Class bodies are not enclosing scopes.** A function nested in a class cannot
see the class body's names::

    class View:
        queryset = Model.objects.all()          # class attribute
        def get(self):
            return queryset                     # NameError, not the attribute

A resolver that walks parents blindly will happily resolve that ``queryset`` to
the class attribute and report an N+1 against a queryset the method never
touches. :meth:`Scope.resolve` skips class scopes for anything that did not
originate directly in them.

**A name assigned anywhere in a function is local to all of it.** Not just after
the assignment. So a function that reads ``qs`` at the top and assigns it at the
bottom is reading a local, not the module global.

**Comprehensions have their own scope, except for the first iterable.** In
``[x.author for x in Book.objects.all()]`` the ``x`` belongs to the
comprehension and the queryset is evaluated in the enclosing scope. This matters
directly: comprehensions are where a large share of real N+1s live.

**A walrus inside a comprehension binds outside it.** PEP 572 sends it to the
enclosing function scope, which is the one place a comprehension leaks.

What this module deliberately does *not* do is flow analysis. :meth:`resolve`
answers "which binding does this name refer to", not "which value does it hold
at line 40" -- reassignment ordering is substep 3.1.2's job. Keeping the two
apart matters, because a scope is a fact about the source text while a value is
a claim about execution, and only one of them is cheap to be certain about.

The size of that gap is measured rather than assumed. Across the three
benchmark targets, every ``for`` loop over a queryset-shaped expression has its
target correctly bound -- 441 of 441 via :meth:`Scope.own_all`. But
:meth:`resolve`, which returns only the last binding of a name, finds the right
one for just 80.6% of them on pretix: roughly one loop variable in five is
rebound later in the same scope. A rule built on :meth:`resolve` alone would
therefore reason about the wrong value one time in five. Use
:meth:`Scope.own_all` and pick by position until 3.1.2 lands.

Names bound by ``from x import *`` are not resolvable here, by design: we
cannot know what a star import brought in without following it. That is
visible on NetBox, where 316 of 1,213 files use one and name resolution inside
functions consequently sits at 90.5% against 99.6% for the other two targets.
:mod:`djaudit.astutils` exposes the star-import targets for rules that need to
decide what to do about it.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator


class ScopeKind(StrEnum):
    """The kind of construct that introduced a scope."""

    MODULE = "module"
    CLASS = "class"
    FUNCTION = "function"
    LAMBDA = "lambda"
    COMPREHENSION = "comprehension"


class BindingKind(StrEnum):
    """How a name came to be bound.

    Rules care about the distinction: a parameter's value is unknown without
    interprocedural analysis, a for-target holds an *element* of something, and
    an import is a fixed reference to another module.
    """

    ASSIGNMENT = "assignment"
    AUGMENTED = "augmented"
    PARAMETER = "parameter"
    IMPORT = "import"
    FOR_TARGET = "for_target"
    COMPREHENSION_TARGET = "comprehension_target"
    WITH_TARGET = "with_target"
    EXCEPT_TARGET = "except_target"
    FUNCTION_DEF = "function_def"
    CLASS_DEF = "class_def"
    WALRUS = "walrus"
    MATCH_TARGET = "match_target"


#: Kinds whose ``value`` is an iterable the name draws one element from.
ELEMENT_KINDS = frozenset({BindingKind.FOR_TARGET, BindingKind.COMPREHENSION_TARGET})


@dataclass(frozen=True, slots=True)
class Binding:
    """One occurrence of a name being bound.

    ``value`` is the expression the binding derives from, and its meaning
    depends on ``kind``:

    ``ASSIGNMENT``
        the right-hand side.
    ``FOR_TARGET``, ``COMPREHENSION_TARGET``
        the *iterable*. The name holds one element of it, flagged by
        :attr:`element_of`. Reading it as the value itself is the difference
        between "this is a queryset" and "this is a model instance", which is
        the whole of N+1 detection.
    ``WITH_TARGET``
        the context manager expression, **not** its ``__enter__`` result. We
        cannot know the latter statically.
    ``PARAMETER``
        the default, when there is one, and ``None`` otherwise.
    ``IMPORT``, ``FUNCTION_DEF``, ``CLASS_DEF``, ``EXCEPT_TARGET``
        ``None``.

    ``unpacked`` marks a name that came out of a tuple or list pattern, where
    ``value`` describes the whole right-hand side rather than this name's share
    of it. Rules should treat an unpacked binding's value as a weak hint.
    """

    name: str
    kind: BindingKind
    node: ast.AST
    value: ast.expr | None = None
    unpacked: bool = False

    @property
    def element_of(self) -> bool:
        """Whether the name holds one element of ``value`` rather than it.

        Derived from :attr:`kind` rather than stored, so the two cannot
        disagree. A ``FOR_TARGET`` that claimed otherwise would be exactly the
        confusion this flag exists to prevent.
        """
        return self.kind in ELEMENT_KINDS

    @property
    def lineno(self) -> int:
        return getattr(self.node, "lineno", 0)


@dataclass(eq=False)
class Scope:
    """A lexical scope and the names bound directly in it."""

    kind: ScopeKind
    node: ast.AST
    name: str
    parent: Scope | None = None
    children: list[Scope] = field(default_factory=list)
    bindings: dict[str, list[Binding]] = field(default_factory=dict)
    declared_global: set[str] = field(default_factory=set)
    declared_nonlocal: set[str] = field(default_factory=set)
    has_loop: bool = False
    """Whether a loop is written directly in this scope.

    Recorded while the scope tree is built, because the builder already visits
    every ``for`` and every comprehension with exactly the scope that owns it.
    Answering the question here costs a flag; answering it afterwards costs a
    second full walk of every tree, which measured slower than the def-use
    work the flag exists to skip.
    """

    def add(self, binding: Binding) -> None:
        home = self._home_for(binding.name)
        home.bindings.setdefault(binding.name, []).append(binding)

    def _home_for(self, name: str) -> Scope:
        """Where a binding of ``name`` made *here* actually lands.

        ``global`` and ``nonlocal`` mean an assignment in this scope rebinds a
        name that lives elsewhere, so the binding is recorded where it really
        goes. Redirecting once, here, is what keeps :meth:`resolve` and
        :meth:`resolve_scope` from disagreeing: a rule asking "is this local?"
        and a rule asking "what does it hold?" must not get answers from two
        different pieces of logic.

        A ``nonlocal`` with no enclosing binding to attach to is a compile-time
        error in real Python but parses cleanly, so it stays here rather than
        being invented into the module scope.
        """
        if name in self.declared_global:
            return self.module()
        if name in self.declared_nonlocal:
            scope = self.parent
            while scope is not None:
                if scope.kind in (ScopeKind.FUNCTION, ScopeKind.LAMBDA) and scope.binds(name):
                    return scope
                scope = scope.parent
        return self

    def child(self, kind: ScopeKind, node: ast.AST, name: str) -> Scope:
        scope = Scope(kind=kind, node=node, name=name, parent=self)
        self.children.append(scope)
        return scope

    @property
    def qualname(self) -> str:
        """Dotted path from the module, for evidence and debugging."""
        if self.parent is None:
            return self.name
        return f"{self.parent.qualname}.{self.name}"

    def own(self, name: str) -> Binding | None:
        """The last binding of ``name`` made directly in this scope."""
        found = self.bindings.get(name)
        return found[-1] if found else None

    def own_all(self, name: str) -> tuple[Binding, ...]:
        """Every binding of ``name`` in this scope, in source order."""
        return tuple(self.bindings.get(name, ()))

    def binds(self, name: str) -> bool:
        return name in self.bindings

    def module(self) -> Scope:
        scope = self
        while scope.parent is not None:
            scope = scope.parent
        return scope

    def enclosing_function(self) -> Scope | None:
        """The nearest scope a walrus or ``nonlocal`` would reach."""
        scope = self.parent
        while scope is not None:
            if scope.kind in (ScopeKind.FUNCTION, ScopeKind.LAMBDA, ScopeKind.MODULE):
                return scope
            scope = scope.parent
        return None

    def visible_scopes(self) -> Iterator[Scope]:
        """This scope, then the ones a name here can actually reach.

        Class scopes are skipped for everything except a name written directly
        in the class body, which is the rule that stops a method resolving its
        class's attributes as if they were locals.
        """
        scope: Scope | None = self
        first = True
        while scope is not None:
            if first or scope.kind is not ScopeKind.CLASS:
                yield scope
            scope = scope.parent
            first = False

    def resolve(self, name: str) -> Binding | None:
        """The binding ``name`` refers to from here, or ``None`` if unbound.

        Returns the *last* binding in the winning scope. That is the right
        answer for the common case of a name bound once, and a starting point
        for substep 3.1.2 when it is bound several times.

        ``global`` and ``nonlocal`` need no special case: :meth:`_home_for`
        already filed those bindings in the scope they belong to.
        """
        for scope in self.visible_scopes():
            found = scope.own(name)
            if found is not None:
                return found
        return None

    def resolve_scope(self, name: str) -> Scope | None:
        """Which scope ``name`` resolves into, without the binding itself."""
        for scope in self.visible_scopes():
            if scope.binds(name):
                return scope
        return None

    def walk(self) -> Iterator[Scope]:
        """This scope and every scope beneath it, depth first."""
        yield self
        for child in self.children:
            yield from child.walk()

    def __repr__(self) -> str:
        return f"<Scope {self.kind} {self.qualname} names={sorted(self.bindings)}>"


def build_scopes(tree: ast.Module, *, name: str = "<module>") -> Scope:
    """Build the scope tree for a parsed module."""
    root = Scope(kind=ScopeKind.MODULE, node=tree, name=name)
    _Builder().statements(root, tree.body)
    return root


class _Builder:
    """Walks a module once, creating scopes and recording bindings.

    Nested scopes are created rather than descended into: the walk of a
    function body happens against that function's own scope, so a name bound
    inside it never lands in the enclosing one.
    """

    def statements(self, scope: Scope, body: list[ast.stmt]) -> None:
        for stmt in body:
            self.statement(scope, stmt)

    def statement(self, scope: Scope, stmt: ast.stmt) -> None:
        match stmt:
            case ast.Assign():
                self.expression(scope, stmt.value)
                for target in stmt.targets:
                    self.target(scope, target, BindingKind.ASSIGNMENT, stmt.value)
            case ast.AnnAssign():
                if stmt.value is not None:
                    self.expression(scope, stmt.value)
                    self.target(scope, stmt.target, BindingKind.ASSIGNMENT, stmt.value)
                elif isinstance(stmt.target, ast.Name):
                    # ``x: int`` declares without binding. Recording it would
                    # make an unassigned annotation look like a definition.
                    pass
            case ast.AugAssign():
                self.expression(scope, stmt.value)
                self.target(scope, stmt.target, BindingKind.AUGMENTED, stmt.value)
            case ast.For() | ast.AsyncFor():
                scope.has_loop = True
                self.expression(scope, stmt.iter)
                self.target(scope, stmt.target, BindingKind.FOR_TARGET, stmt.iter)
                self.statements(scope, stmt.body)
                self.statements(scope, stmt.orelse)
            case ast.While() | ast.If():
                self.expression(scope, stmt.test)
                self.statements(scope, stmt.body)
                self.statements(scope, stmt.orelse)
            case ast.With() | ast.AsyncWith():
                self.with_stmt(scope, stmt)
            case ast.Try() | ast.TryStar():
                self.try_stmt(scope, stmt)
            case ast.FunctionDef() | ast.AsyncFunctionDef():
                self.function(scope, stmt)
            case ast.ClassDef():
                self.class_def(scope, stmt)
            case ast.Import() | ast.ImportFrom():
                for alias in stmt.names:
                    bound = alias.asname or alias.name.split(".")[0]
                    if bound != "*":
                        scope.add(Binding(bound, BindingKind.IMPORT, alias))
            case ast.Global():
                scope.declared_global.update(stmt.names)
            case ast.Nonlocal():
                scope.declared_nonlocal.update(stmt.names)
            case ast.Match():
                self.match(scope, stmt)
            case ast.Return() | ast.Expr() | ast.Delete() | ast.Assert() | ast.Raise():
                # Nothing binds here; we walk them only for the lambdas,
                # comprehensions and walruses that can hide in an expression.
                for child in ast.iter_child_nodes(stmt):
                    if isinstance(child, ast.expr):
                        self.expression(scope, child)
            case _:
                pass

    def with_stmt(self, scope: Scope, stmt: ast.With | ast.AsyncWith) -> None:
        for item in stmt.items:
            self.expression(scope, item.context_expr)
            if item.optional_vars is not None:
                self.target(scope, item.optional_vars, BindingKind.WITH_TARGET, item.context_expr)
        self.statements(scope, stmt.body)

    def try_stmt(self, scope: Scope, stmt: ast.Try | ast.TryStar) -> None:
        self.statements(scope, stmt.body)
        for handler in stmt.handlers:
            if handler.type is not None:
                self.expression(scope, handler.type)
            if handler.name is not None:
                scope.add(Binding(handler.name, BindingKind.EXCEPT_TARGET, handler))
            self.statements(scope, handler.body)
        self.statements(scope, stmt.orelse)
        self.statements(scope, stmt.finalbody)

    def match(self, scope: Scope, stmt: ast.Match) -> None:
        self.expression(scope, stmt.subject)
        for case in stmt.cases:
            for node in ast.walk(case.pattern):
                captured = getattr(node, "name", None) or getattr(node, "rest", None)
                if isinstance(captured, str):
                    scope.add(Binding(captured, BindingKind.MATCH_TARGET, node))
            if case.guard is not None:
                self.expression(scope, case.guard)
            self.statements(scope, case.body)

    def function(self, scope: Scope, stmt: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        """Bind the function's name here, then walk its body in a new scope.

        Decorators, defaults and annotations are evaluated in the *enclosing*
        scope, at definition time -- a default referring to a name is reading
        the outer one, not a local of the function it is attached to.
        """
        for decorator in stmt.decorator_list:
            self.expression(scope, decorator)
        args = stmt.args
        for default in [*args.defaults, *(d for d in args.kw_defaults if d is not None)]:
            self.expression(scope, default)
        scope.add(Binding(stmt.name, BindingKind.FUNCTION_DEF, stmt))

        inner = scope.child(ScopeKind.FUNCTION, stmt, stmt.name)
        self.parameters(inner, args)
        self.statements(inner, stmt.body)

    def parameters(self, scope: Scope, args: ast.arguments) -> None:
        positional = [*args.posonlyargs, *args.args]
        # Defaults fill the positional list from the right.
        offset = len(positional) - len(args.defaults)
        for index, arg in enumerate(positional):
            default = args.defaults[index - offset] if index >= offset else None
            scope.add(Binding(arg.arg, BindingKind.PARAMETER, arg, default))
        for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
            scope.add(Binding(arg.arg, BindingKind.PARAMETER, arg, default))
        for extra in (args.vararg, args.kwarg):
            if extra is not None:
                scope.add(Binding(extra.arg, BindingKind.PARAMETER, extra))

    def class_def(self, scope: Scope, stmt: ast.ClassDef) -> None:
        for decorator in stmt.decorator_list:
            self.expression(scope, decorator)
        for base in stmt.bases:
            self.expression(scope, base)
        for keyword in stmt.keywords:
            self.expression(scope, keyword.value)
        scope.add(Binding(stmt.name, BindingKind.CLASS_DEF, stmt))

        inner = scope.child(ScopeKind.CLASS, stmt, stmt.name)
        self.statements(inner, stmt.body)

    def expression(self, scope: Scope, node: ast.expr) -> None:
        """Walk an expression for the scopes and bindings hiding inside it."""
        match node:
            case ast.Lambda():
                for default in [
                    *node.args.defaults,
                    *(d for d in node.args.kw_defaults if d is not None),
                ]:
                    self.expression(scope, default)
                inner = scope.child(ScopeKind.LAMBDA, node, "<lambda>")
                self.parameters(inner, node.args)
                self.expression(inner, node.body)
            case ast.ListComp() | ast.SetComp() | ast.GeneratorExp():
                inner = self.comprehension(scope, node, node.generators)
                self.expression(inner, node.elt)
            case ast.DictComp():
                inner = self.comprehension(scope, node, node.generators)
                self.expression(inner, node.key)
                self.expression(inner, node.value)
            case ast.NamedExpr():
                # PEP 572: a walrus inside a comprehension binds in the
                # enclosing function, which is the one way a comprehension
                # leaks a name.
                target = scope
                while target.kind is ScopeKind.COMPREHENSION:
                    target = target.enclosing_function() or target.module()
                self.expression(scope, node.value)
                self.target(target, node.target, BindingKind.WALRUS, node.value)
            case _:
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, ast.expr):
                        self.expression(scope, child)
                    elif isinstance(child, ast.keyword):
                        # A call holds each keyword argument under an
                        # `ast.keyword`, which is not an expression, so an
                        # expression filter walks past the whole argument.
                        # `sorted(rows, key=lambda r: r.n)` and
                        # `Prefetch("x", queryset=[q for q in qs])` are both
                        # written this way, and neither inner scope existed:
                        # 670 lambdas and comprehensions across the three
                        # benchmark corpora had no scope object at all.
                        self.expression(scope, child.value)

    def comprehension(
        self, scope: Scope, node: ast.expr, generators: list[ast.comprehension]
    ) -> Scope:
        """Create the comprehension's scope and bind its targets.

        Only the *first* generator's iterable is evaluated in the enclosing
        scope. Every later one sees the names the earlier generators bound.
        """
        # A comprehension is owned by the scope it is *written in*, matching
        # how `find_loops` attributes one, not by the scope it creates.
        scope.has_loop = True
        inner = scope.child(ScopeKind.COMPREHENSION, node, "<comprehension>")
        for index, generator in enumerate(generators):
            outer = scope if index == 0 else inner
            self.expression(outer, generator.iter)
            self.target(
                inner,
                generator.target,
                BindingKind.COMPREHENSION_TARGET,
                generator.iter,
            )
            for condition in generator.ifs:
                self.expression(inner, condition)
        return inner

    def target(
        self,
        scope: Scope,
        target: ast.expr,
        kind: BindingKind,
        value: ast.expr | None,
        *,
        unpacked: bool = False,
    ) -> None:
        """Bind every name in an assignment target.

        ``obj.attr = x`` and ``obj[k] = x`` bind no local name; they are walked
        as expressions so anything nested in them is still seen.
        """
        match target:
            case ast.Name():
                scope.add(Binding(target.id, kind, target, value, unpacked))
            case ast.Tuple() | ast.List():
                for element in target.elts:
                    self.target(scope, element, kind, value, unpacked=True)
            case ast.Starred():
                self.target(scope, target.value, kind, value, unpacked=True)
            case _:
                self.expression(scope, target)

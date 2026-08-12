"""What the code declares, so a repair cannot quietly be a deletion.

The failure this module exists to catch is the obvious one, and it is obvious
only in hindsight. A loop that iterates until the auditor is silent has a
trivial optimum: an empty file. Every finding is in code, so deleting the code
clears every finding. A model asked to fix `DJD-002` on a nullable `CharField`
can drop the field; asked to fix `DJA-001` on an unauthenticated viewset, it
can drop the viewset. Both produce a clean audit and neither is a repair.

Telling the model not to is necessary and not sufficient -- the prompt says so,
and a prompt is a request. So the check is mechanical: extract what each
version of the app *declares*, and refuse an iteration that declares less.

**Names, not counts.** An early version compared counts, which is cheaper and
wrong in a way that matters: a model that deletes ``Order`` and adds
``OrderAudit`` keeps the count and has still lost the feature. Comparing sets
of names catches that, and it also produces a message that names the casualty
rather than reporting a number that went down.

**Renaming is indistinguishable from deletion here, and that is deliberate.**
``Order`` becoming ``PurchaseOrder`` reads as a loss. Rather than guess at
similarity, the loop reports it as a regression and stops, because a generator
that renames the caller's models mid-repair is doing something the caller
should see.

Fields are tracked per model rather than globally, since a field moving from
one model to another is a schema change and not a rename.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

# Assignments that are Django model fields. Matched on the call being an
# attribute access ending in something that looks like a field, which is loose
# on purpose: this is a regression detector, and a false *positive* here only
# means an extra name is tracked consistently in both versions.
_FIELD_SUFFIX = "Field"

# Base names that make a class part of the app's surface. A model, a
# serializer, a view and an admin are the four things a Django app declares
# that a user can lose.
_TRACKED_BASES = (
    "Model",
    "Serializer",
    "ModelSerializer",
    "HyperlinkedModelSerializer",
    "ViewSet",
    "ModelViewSet",
    "ReadOnlyModelViewSet",
    "GenericViewSet",
    "APIView",
    "ListAPIView",
    "RetrieveAPIView",
    "CreateAPIView",
    "UpdateAPIView",
    "DestroyAPIView",
    "ListCreateAPIView",
    "RetrieveUpdateAPIView",
    "RetrieveUpdateDestroyAPIView",
    "ModelAdmin",
    "TabularInline",
    "StackedInline",
)


@dataclass(frozen=True)
class Surface:
    """Everything a version of the app declares that could be lost."""

    classes: frozenset[str] = frozenset()
    fields: frozenset[tuple[str, str]] = frozenset()
    functions: frozenset[str] = frozenset()

    @property
    def empty(self) -> bool:
        return not (self.classes or self.fields or self.functions)

    def missing_from(self, later: Surface) -> Regression:
        """What ``later`` no longer declares."""
        return Regression(
            classes=tuple(sorted(self.classes - later.classes)),
            fields=tuple(sorted(self.fields - later.fields)),
            functions=tuple(sorted(self.functions - later.functions)),
        )


@dataclass(frozen=True)
class Regression:
    """Names that were there and are not."""

    classes: tuple[str, ...] = ()
    fields: tuple[tuple[str, str], ...] = ()
    functions: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.classes or self.fields or self.functions)

    def describe(self) -> str:
        """Name the casualties. A count would not be actionable."""
        parts: list[str] = []
        if self.classes:
            parts.append(f"classes removed: {', '.join(self.classes)}")
        if self.fields:
            lost = ", ".join(f"{owner}.{name}" for owner, name in self.fields)
            parts.append(f"fields removed: {lost}")
        if self.functions:
            parts.append(f"functions removed: {', '.join(self.functions)}")
        return "; ".join(parts)


@dataclass
class _Collector(ast.NodeVisitor):
    """Walks one module, recording what it declares."""

    classes: set[str] = field(default_factory=set)
    fields: set[tuple[str, str]] = field(default_factory=set)
    functions: set[str] = field(default_factory=set)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if _is_tracked(node):
            self.classes.add(node.name)
            for statement in node.body:
                self._record_field(node.name, statement)
        # Nested classes are Meta, Inline configs and the like -- descended
        # into so an inline admin declared inside another is not lost.
        for statement in node.body:
            if isinstance(statement, ast.ClassDef):
                self.visit(statement)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        # Module-level only. A method disappearing from a tracked class is a
        # behaviour change this detector deliberately does not police, because
        # rewriting a method body is exactly what a repair does.
        self.functions.add(node.name)

    def _record_field(self, owner: str, statement: ast.stmt) -> None:
        if not isinstance(statement, ast.Assign):
            return
        if not isinstance(statement.value, ast.Call):
            return
        if not _looks_like_field(statement.value.func):
            return
        for target in statement.targets:
            if isinstance(target, ast.Name):
                self.fields.add((owner, target.id))


def _is_tracked(node: ast.ClassDef) -> bool:
    for base in node.bases:
        name = base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", "")
        if name in _TRACKED_BASES:
            return True
    return False


def _looks_like_field(func: ast.expr) -> bool:
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    return name.endswith(_FIELD_SUFFIX) or name in {
        "ForeignKey",
        "ManyToManyField",
        "OneToOneField",
    }


def surface_of(sources: dict[str, str]) -> Surface:
    """The combined surface of a set of files.

    A file that does not parse contributes nothing rather than raising. The
    loop already refuses unparseable output on its own terms, and a detector
    that crashed on bad input would turn a clear syntax error into a traceback
    from an unrelated module.
    """
    collector = _Collector()
    for source in sources.values():
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for node in tree.body:
            collector.visit(node)
    return Surface(
        classes=frozenset(collector.classes),
        fields=frozenset(collector.fields),
        functions=frozenset(collector.functions),
    )

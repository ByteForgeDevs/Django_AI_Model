"""Check the model graph against the project's own migrations.

The fixtures measure the graph against a manifest somebody wrote, which is
worth exactly as much as the person writing it. This measures it against
Django's own record: every ``CreateModel``, ``AddField``, ``RemoveField`` and
``RenameField`` a project has ever run, replayed in order to produce the set of
tables and columns that actually exist. Django wrote those files by
introspecting the models at runtime, with every third-party package installed
and every base class resolved -- which is precisely what a static reader
cannot do, so it is an independent answer rather than a second opinion from the
same source.

The number that matters is not coverage. It is how much of the shortfall we
can *account for*: a graph that misses fields it cannot possibly see is
working correctly, and a graph that misses one field nobody can explain has a
bug. So every discrepancy is attributed to a cause, and the gate is on the
ones left over.

Migrations are read with ``ast`` and never imported -- the same rule the rest
of the tool follows.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from djaudit import engine
from djaudit.graph.inheritance import ClassIndex
from djaudit.graph.nodes import ModelGraph, ModelNode

NOT_A_COLUMN = frozenset({"GenericRelation", "GenericForeignKey"})
"""Field kinds that never appear in a migration because they are not columns.

``GenericRelation`` is a reverse accessor and ``GenericForeignKey`` is two
existing columns read together. Both belong in the graph -- a rule asking what
a model can reach needs them -- and neither has a table to be in.
"""

DJANGO_ABSTRACT_BASES = frozenset(
    {
        "AbstractBaseUser",
        "AbstractUser",
        "PermissionsMixin",
        "AbstractBaseSession",
    }
)
"""Abstract bases Django ships. A project checkout does not contain Django, so
a class inheriting one of these is recognised by name and its inherited fields
are not readable at all -- ``users.User`` gets ``password`` and ``last_login``
from a file that is not in the repository being scanned."""


@dataclass
class Model:
    """One model as the migrations describe it."""

    name: str
    fields: set[str] = field(default_factory=set)
    proxy: bool = False


def dotted(node: ast.expr) -> str | None:
    parts: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return ".".join(reversed(parts))


def argument(call: ast.Call, position: int, name: str) -> str | None:
    """One operation argument, given either way round.

    ``makemigrations`` writes keywords and hand-edited migrations write
    positionals, and both are in every large project's history.
    """
    found: ast.expr | None = None
    for keyword in call.keywords:
        if keyword.arg == name:
            found = keyword.value
    if found is None and len(call.args) > position:
        found = call.args[position]
    return found.value if isinstance(found, ast.Constant) and isinstance(found.value, str) else None


def option(call: ast.Call, name: str) -> bool:
    options = next((k.value for k in call.keywords if k.arg == "options"), None)
    if not isinstance(options, ast.Dict):
        return False
    for key, value in zip(options.keys, options.values, strict=True):
        if isinstance(key, ast.Constant) and key.value == name and isinstance(value, ast.Constant):
            return bool(value.value)
    return False


def created_fields(call: ast.Call) -> set[str]:
    listed = next((k.value for k in call.keywords if k.arg == "fields"), None)
    if listed is None and len(call.args) > 1:
        listed = call.args[1]
    if not isinstance(listed, ast.List):
        return set()
    names: set[str] = set()
    for entry in listed.elts:
        if not isinstance(entry, ast.Tuple) or not entry.elts:
            continue
        first = entry.elts[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            names.add(first.value)
    return names


def app_label(app_dir: Path) -> str:
    """The label Django will file this app's models under.

    Not the directory name. pretix's ``src/pretix/base`` declares
    ``label = 'pretixbase'`` in its ``AppConfig``, and every migration and
    every ``ForeignKey('pretixbase.Order')`` in the project uses that instead.
    Read from ``apps.py`` here rather than borrowed from djaudit's own
    discovery, because an oracle that shares the code under test is not an
    oracle.
    """
    source = app_dir / "apps.py"
    if not source.is_file():
        return app_dir.name
    try:
        tree = ast.parse(source.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return app_dir.name
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for statement in node.body:
            if not isinstance(statement, ast.Assign) or not isinstance(
                statement.value, ast.Constant
            ):
                continue
            named = [t.id for t in statement.targets if isinstance(t, ast.Name)]
            if "label" in named and isinstance(statement.value.value, str):
                return statement.value.value
    return app_dir.name


def replay(root: Path) -> dict[str, dict[str, Model]]:
    """Every migration in the project, applied in order, per app.

    Keyed on the lowercased model name because that is how Django refers to a
    model in ``AddField``, and case has drifted in a few places over the years.
    """
    apps: dict[str, dict[str, Model]] = {}
    for directory in sorted(root.rglob("migrations")):
        if not directory.is_dir():
            continue
        files = sorted(p for p in directory.glob("*.py") if p.name != "__init__.py")
        if not files:
            continue
        models = apps.setdefault(app_label(directory.parent), {})
        for path in files:
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:
                continue
            for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call)):
                apply_operation(models, call, (dotted(call.func) or "").rsplit(".", 1)[-1])
    return apps


def apply_operation(models: dict[str, Model], call: ast.Call, operation: str) -> None:
    if operation == "CreateModel":
        name = argument(call, 0, "name")
        if name is not None:
            models[name.lower()] = Model(name, created_fields(call), option(call, "proxy"))
    elif operation == "DeleteModel":
        name = argument(call, 0, "name")
        if name is not None:
            models.pop(name.lower(), None)
    elif operation == "RenameModel":
        old, new = argument(call, 0, "old_name"), argument(call, 1, "new_name")
        if old is not None and new is not None and old.lower() in models:
            moved = models.pop(old.lower())
            moved.name = new
            models[new.lower()] = moved
    elif operation == "AddField":
        model, name = argument(call, 0, "model_name"), argument(call, 1, "name")
        if model is not None and name is not None and model.lower() in models:
            models[model.lower()].fields.add(name)
    elif operation == "RemoveField":
        model, name = argument(call, 0, "model_name"), argument(call, 1, "name")
        if model is not None and name is not None and model.lower() in models:
            models[model.lower()].fields.discard(name)
    elif operation == "RenameField":
        model = argument(call, 0, "model_name")
        old, new = argument(call, 1, "old_name"), argument(call, 2, "new_name")
        if model is not None and old is not None and new is not None and model.lower() in models:
            models[model.lower()].fields.discard(old)
            models[model.lower()].fields.add(new)


def why_absent(index: ClassIndex, name: str) -> str | None:
    """Why a model in the migrations is missing from the graph, if we can say.

    Two answers are honest and one is not.

    A class the graph never contained cannot be explained by asking the graph,
    which is the shape ``extras.TaggedItem`` has: its single base is
    django-taggit's ``GenericTaggedItemBase``, so nothing about it says
    "model" to a reader who has only the project's own source.

    And a name with no ``class`` statement behind it anywhere was never
    written down at all. pretix's ``Event_SettingsStore`` is created by
    django-hierarkey's ``@settings_hierarkey.add`` decorator while the module
    imports, and appears in pretix's source exactly once, in an import list.
    No static reader can find it, and one that claimed to would be reading the
    migrations rather than the code.

    ``None`` means we have no account of it, which is the only answer that
    should fail a build.
    """
    declared = [record for record in index.records() if record.name == name]
    if not declared:
        return "no class statement declares it -- generated while the module imports"
    if any(index.unresolved_bases(record) for record in declared):
        return "inherits a base outside the project"
    return None


def unreadable_ancestry(graph: ModelGraph, model: ModelNode) -> tuple[str, ...]:
    """Bases of this model, or of any project ancestor, that we could not read.

    Attribution has to walk the MRO rather than stop at the class: NetBox's
    ``Region`` inherits ``NestedGroupModel``, which is NetBox's own and is
    perfectly readable, and it is *that* class which inherits django-mptt's
    ``MPTTModel``. The five tree columns arrive from two levels up.
    """
    found: list[str] = []
    for label in (model.label, *model.mro):
        found.extend(graph.unresolved_bases.get(label, ()))
    found.extend(base for base in model.bases if base in DJANGO_ABSTRACT_BASES)
    return tuple(dict.fromkeys(found))


@dataclass
class Coverage:
    """What the migrations say, what the graph found, and why they differ."""

    models_expected: int = 0
    models_found: int = 0
    models_as_proxy: int = 0
    models_unreadable_base: list[str] = field(default_factory=list)
    models_unexplained: list[str] = field(default_factory=list)

    fields_expected: int = 0
    fields_found: int = 0
    fields_unreadable_base: Counter[str] = field(default_factory=Counter)
    fields_unexplained: list[str] = field(default_factory=list)

    relations: int = 0
    relations_resolved: int = 0
    relations_outside: Counter[str] = field(default_factory=Counter)
    not_a_column: int = 0

    @property
    def model_coverage(self) -> float:
        return self.models_found / self.models_expected if self.models_expected else 1.0

    @property
    def field_coverage(self) -> float:
        return self.fields_found / self.fields_expected if self.fields_expected else 1.0


def measure(root: Path) -> Coverage:
    apps = replay(root)
    context = engine.run(root).context
    graph = context.model_graph
    index = ClassIndex(context)
    out = Coverage()

    for app, models in apps.items():
        for record in models.values():
            label = f"{app}.{record.name}"
            out.models_expected += 1
            node = graph.get(label)
            if node is None:
                reason = why_absent(index, record.name)
                if reason is None:
                    out.models_unexplained.append(label)
                else:
                    out.models_unreadable_base.append(f"{label}: {reason}")
                continue
            out.models_found += 1
            if node.is_proxy:
                out.models_as_proxy += 1
            measure_fields(graph, node, record, out)

    for model in graph.models.values():
        for edge in model.relations:
            target = edge.target_ref
            # A GenericForeignKey has no target by construction -- it is two
            # columns read together -- so counting it as unresolved would
            # invent a failure.
            if target is None:
                continue
            out.relations += 1
            if target == "self" or graph.get(target) is not None:
                out.relations_resolved += 1
            else:
                out.relations_outside[target] += 1
        out.not_a_column += sum(
            1 for f in {**model.fields, **model.inherited}.values() if f.kind in NOT_A_COLUMN
        )
    return out


def measure_fields(graph: ModelGraph, node: ModelNode, record: Model, out: Coverage) -> None:
    # `id` is added by Django rather than written by anyone, and a model that
    # declares its own primary key has no `id` at all -- counting it would
    # measure Django's behaviour, not ours.
    expected = {name for name in record.fields if name != "id"}
    present = set(node.fields) | set(node.inherited)
    out.fields_expected += len(expected)
    out.fields_found += len(expected & present)
    if missing := expected - present:
        unreadable = unreadable_ancestry(graph, node)
        for name in sorted(missing):
            if unreadable:
                out.fields_unreadable_base[unreadable[0]] += 1
            else:
                out.fields_unexplained.append(f"{node.label}.{name}")


def report(name: str, out: Coverage) -> None:
    print(f"{name}: model graph against its own migrations")
    print(
        f"  models   {out.models_found}/{out.models_expected} "
        f"({out.model_coverage:.1%}), {out.models_as_proxy} of them proxies"
    )
    print(f"  fields   {out.fields_found}/{out.fields_expected} ({out.field_coverage:.1%})")
    print(f"  relations {out.relations_resolved}/{out.relations} point at a model the graph has")
    if out.relations_outside:
        named = ", ".join(f"{t} x{n}" for t, n in out.relations_outside.most_common(6))
        print(f"  pointing outside the project: {named}")
    print(f"  {out.not_a_column} graph fields are not columns and cannot appear in a migration")
    for base, count in sorted(out.fields_unreadable_base.items(), key=lambda kv: -kv[1]):
        print(f"  accounted for: {count} fields inherited from {base}, which is not in the project")
    for entry in out.models_unreadable_base:
        print(f"  accounted for: {entry}")
    if out.models_unexplained:
        print(f"  UNEXPLAINED models: {', '.join(out.models_unexplained)}")
    if out.fields_unexplained:
        shown = ", ".join(out.fields_unexplained[:20])
        print(f"  UNEXPLAINED fields ({len(out.fields_unexplained)}): {shown}")


def check(out: Coverage, expected: dict[str, object]) -> list[str]:
    """Compare against recorded numbers. Coverage may rise, never fall."""
    failures: list[str] = []
    floors = {
        "models_found": out.models_found,
        "fields_found": out.fields_found,
        "relations_resolved": out.relations_resolved,
    }
    for key, actual in floors.items():
        floor = expected.get(key)
        if isinstance(floor, int) and actual < floor:
            failures.append(f"{key} fell from {floor} to {actual}")
    ceilings = {
        "models_unexplained": len(out.models_unexplained),
        "fields_unexplained": len(out.fields_unexplained),
    }
    for key, actual in ceilings.items():
        ceiling = expected.get(key)
        if isinstance(ceiling, int) and actual > ceiling:
            failures.append(f"{key} rose from {ceiling} to {actual}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path)
    parser.add_argument("--name", default=None)
    parser.add_argument("--expect", type=Path, default=None)
    parser.add_argument("--write", type=Path, default=None)
    args = parser.parse_args()

    name = args.name or args.target.name
    out = measure(args.target)
    report(name, out)

    if args.write is not None:
        args.write.write_text(
            json.dumps(
                {
                    "target": name,
                    "models_expected": out.models_expected,
                    "models_found": out.models_found,
                    "fields_expected": out.fields_expected,
                    "fields_found": out.fields_found,
                    "relations_resolved": out.relations_resolved,
                    "models_unexplained": len(out.models_unexplained),
                    "fields_unexplained": len(out.fields_unexplained),
                },
                indent=2,
            )
            + "\n"
        )

    if args.expect is not None:
        failures = check(out, json.loads(args.expect.read_text()))
        if failures:
            for failure in failures:
                print(f"  REGRESSED: {failure}")
            return 1
        print("  graph coverage holds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

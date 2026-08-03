"""Reading the project's URL configuration.

Phase 1 needs only one question answered — where the admin is mounted — but
the `DJA` family in Phase 2 is entirely about routes and the views behind them,
so this reads the urlconf as a whole rather than grepping for one pattern.

What it deliberately does not do is resolve ``include()`` into other modules or
reconstruct full URLs. A route's prefix comes from wherever it was included,
which can be several files away and conditional on the way; guessing at it
would produce confident, wrong paths. Each route is reported as it is written,
with a flag saying whether the pattern was fully literal, and rules decide for
themselves how much of that they can use.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from djaudit.context import ProjectContext

ROUTERS = frozenset({"path", "re_path", "url"})
"""The three ways Django has spelled "map this pattern to this view"."""


@dataclass(frozen=True, slots=True)
class Route:
    """One pattern-to-view mapping, as written."""

    pattern: str
    """The literal part of the pattern. Interpolated pieces contribute nothing."""

    literal: bool
    """Whether the whole pattern was literal, so :attr:`pattern` is all of it."""

    router: str
    view: str
    """The view argument, unparsed -- ``admin.site.urls``, ``include('x.urls')``."""

    path: Path
    line: int
    node: ast.Call


def pattern_of(node: ast.expr) -> tuple[str, bool]:
    """The literal text of a route pattern, and whether that is all of it.

    Healthchecks is why the partial answer exists: it writes
    ``path(f"{prefix}admin/", admin.site.urls)`` where the prefix comes from a
    setting. The tail is knowable and the whole is not, and a reader that
    insisted on certainty would be blind to a very common shape.
    """
    if isinstance(node, ast.Constant):
        return (node.value, True) if isinstance(node.value, str) else ("", False)
    if isinstance(node, ast.JoinedStr):
        text = "".join(
            part.value
            for part in node.values
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        )
        return text, all(isinstance(part, ast.Constant) for part in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, left_ok = pattern_of(node.left)
        right, right_ok = pattern_of(node.right)
        return left + right, left_ok and right_ok
    return "", False


def routes(ctx: ProjectContext, path: Path) -> tuple[Route, ...]:
    """Every route mapped in one urlconf module.

    Every ``path``/``re_path``/``url`` call in the file counts, wherever it
    sits. Real urlconfs build ``urlpatterns`` by concatenation, in ``if``
    branches, and through helper functions, and a reader that only understood
    the one assignment would miss most of them for no gain in accuracy: a route
    written in a file that Django imports is a route.
    """
    tree = ctx.parse(path)
    if tree is None:
        return ()
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or len(node.args) < 2:
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name not in ROUTERS:
            continue
        pattern, literal = pattern_of(node.args[0])
        found.append(
            Route(
                pattern=pattern,
                literal=literal,
                router=name,
                view=ast.unparse(node.args[1]),
                path=path,
                line=node.args[0].lineno,
                node=node,
            )
        )
    return tuple(found)


def locate(ctx: ProjectContext, dotted: str) -> Path | None:
    """The file a dotted module name refers to, inside the analysed tree.

    Not simply ``root / dotted``: a project's importable root is often a
    directory below the repository root -- NetBox's ``netbox.urls`` lives at
    ``netbox/netbox/urls.py`` -- so the name is matched as a suffix of the
    paths we already found, nearest the root winning.
    """
    if not dotted:
        return None
    relative = Path(*dotted.split("."))
    wanted = (relative.with_suffix(".py").as_posix(), (relative / "__init__.py").as_posix())
    matches = [
        candidate
        for candidate in ctx.python_files
        for target in wanted
        if candidate.as_posix() == (ctx.root / target).as_posix()
        or candidate.as_posix().endswith("/" + target)
    ]
    return min(matches, key=lambda p: (len(p.parts), p.as_posix())) if matches else None


def urlconfs(ctx: ProjectContext, names: Iterator[str]) -> tuple[Path, ...]:
    """The distinct urlconf files named by a set of settings modules."""
    found: dict[str, Path] = {}
    for name in names:
        path = locate(ctx, name)
        if path is not None:
            found.setdefault(path.as_posix(), path)
    return tuple(found.values())

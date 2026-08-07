"""`DJI-012` -- a file opened at a path the request supplies.

Four measurements shaped this rule, and two of them are about which names to
trust rather than about traversal itself.

**A constant base is no defence, and here even concatenation loses.** Measured
by construction against ``/var/data``::

    os.path.join(BASE, "report.pdf")        -> /var/data/report.pdf
    os.path.join(BASE, "../../etc/passwd")  -> /etc/passwd     (after normpath)
    os.path.join(BASE, "/etc/passwd")       -> /etc/passwd     (base discarded)
    Path(BASE) / "/etc/passwd"              -> /etc/passwd     (base discarded)

An absolute payload makes ``join`` throw the base away outright, and ``..``
escapes it after normalisation. This is `DJI-009`'s ``urljoin`` finding again
with one difference that matters: there, plain ``BASE + p`` was *safe*, because
no payload can move a URL's authority once a host has been written. A path has
no authority, so ``..`` climbs out of a concatenation just as easily. That is
why this rule asks about **every** part of a path and `DJI-009` asks only about
the part that reaches the host.

**Django's storage API already refuses this, so it is not reported.**
``FileSystemStorage.path`` is ``safe_join(self.location, name)``, and measured
by construction ``default_storage.open("../../etc/passwd")`` and
``default_storage.open("/etc/passwd")`` both raise ``SuspiciousFileOperation``
while ``open("ok.txt")`` returns the file. A rule that flagged storage reads
would be reporting the framework's own defence as the defect.

**``open`` is a sink only when it is bare, and the corpus is emphatic.** Across
the three benchmarks there are 58 bare ``open(...)`` calls -- the builtin -- and
39 written as an attribute, of which 12 are ``default_storage.open`` and the
rest belong to PIL, ``tarfile`` and ``pathlib``. Reading the immediate holder,
the discipline `DJI-007` through `DJI-009` already use, is what separates them.

**The filesystem mutators are the same lesson at the opposite polarity.**
Matching ``remove``, ``unlink``, ``rename``, ``copy``, ``copyfile``, ``move`` or
``rmtree`` by name finds **209 calls across the corpus of which exactly one is
``os`` or ``shutil``**: 80 are ``copy.copy`` from the copy module and most of
the remainder are ``list.remove()`` on an ordinary list. So ``open`` is trusted
only bare and these are trusted only when attributed to ``os`` or ``shutil`` --
opposite rules for the two groups, each one measured rather than assumed.

Like `DJI-011`, this rule carries no list of sanitiser names. ``basename()`` and
``safe_join()`` are declined for the same reason a project's own helper is: they
are calls, and :func:`~djaudit.rules._injection.reads_request` reports only the
request being read. A name-keyed sanitiser list would sit unreachable behind
that test. ``os.path.join`` is the exception the rule does look inside, because
like ``str.format`` it is not opaque -- its arguments appear in the result.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from djaudit.dataflow.chains import def_use
from djaudit.dataflow.taint import Taint, request_source, taint_of
from djaudit.models import (
    Confidence,
    Evidence,
    EvidenceKind,
    Family,
    Finding,
    Severity,
    Tier,
)
from djaudit.registry import Rule, RuleMeta, register
from djaudit.rules._injection import own_nodes, reads_request

if TYPE_CHECKING:
    from pathlib import Path as FsPath

    from djaudit.context import ProjectContext
    from djaudit.dataflow.chains import DefUse
    from djaudit.dataflow.scopes import Scope

BUILTIN = "open"
"""The builtin. Matched bare only.

39 of the corpus's 97 calls named ``open`` are attributes, and every one of
them belongs to something that is not the builtin -- Django storage, PIL,
``tarfile``, ``pathlib``. Django's storage in particular refuses traversal on
its own, so matching an attribute here would report its defence as a defect.
"""

FILESYSTEM = frozenset(
    {"remove", "unlink", "rename", "renames", "rmdir", "removedirs", "listdir", "mkdir", "makedirs"}
)
"""``os`` operations that act on a path. Matched only on ``os``."""

SHUTIL = frozenset({"copy", "copy2", "copyfile", "copytree", "move", "rmtree"})
"""``shutil`` operations that act on a path. Matched only on ``shutil``."""

MODULES = frozenset({"os", "shutil"})
"""The only holders those names are trusted on.

Matched by name alone they find 209 corpus calls and one of them is a real
filesystem operation: 80 are ``copy.copy`` and most of the rest are
``list.remove()``.
"""

JOINS = frozenset({"join"})
"""``os.path.join`` and its posix and nt twins, which splice verbatim."""

JOIN_HOLDERS = frozenset({"path", "posixpath", "ntpath", "genericpath"})
"""What ``join`` has to be attributed to before it is read as a path join."""

WORDS = ("open(", "os.", "shutil.", "Path(")
"""A necessary condition on a file's text: no sink can be spelled without one."""


@dataclass(frozen=True, slots=True)
class Access:
    """One filesystem access, and the expression supplying its path."""

    call: ast.Call
    path: ast.expr
    name: str


def accesses(node: ast.Call) -> Iterator[Access]:
    """The filesystem accesses this call performs, if it performs any."""
    function = node.func
    if isinstance(function, ast.Name):
        if function.id == BUILTIN and node.args:
            yield Access(node, node.args[0], BUILTIN)
        return

    if not isinstance(function, ast.Attribute) or not node.args:
        return
    holder = function.value
    if not isinstance(holder, ast.Name) or holder.id not in MODULES:
        return
    if holder.id == "os" and function.attr in FILESYSTEM:
        yield Access(node, node.args[0], f"os.{function.attr}")
    elif holder.id == "shutil" and function.attr in SHUTIL:
        for argument in node.args[:2]:
            yield Access(node, argument, f"shutil.{function.attr}")


def traversable(
    node: ast.expr, chains: DefUse | None, seen: set[str] | None = None
) -> ast.expr | None:
    """The part of this path the request supplies, if any part of it does.

    Every part is asked. Unlike a URL, where a host once written cannot be
    displaced, ``..`` climbs out of a path from wherever it appears.
    """
    if seen is None:
        seen = set()

    if isinstance(node, ast.Call):
        if reads_request(node, chains):
            return node
        return _joined(node, chains, seen)

    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add | ast.Mod | ast.Div):
        # Add concatenates, Mod interpolates, and Div is Path(base) / part --
        # measured above, an absolute part discards the base in all three.
        return _any_part((node.left, node.right), chains, seen)

    if isinstance(node, ast.Tuple | ast.List):
        return _any_part(tuple(node.elts), chains, seen)

    if isinstance(node, ast.JoinedStr):
        return _any_part(tuple(node.values), chains, seen)

    if isinstance(node, ast.Name) and chains is not None:
        # Declining on a repeat visit rather than falling through to taint, for
        # the reason DJI-011 records: a permissive default reverses the guard.
        if node.id in seen:
            return None
        seen.add(node.id)
        for binding in chains.reaching(node):
            if binding.value is None:
                continue
            found = traversable(binding.value, chains, seen)
            if found is not None:
                return found
        return None

    return node if taint_of(node, chains) is Taint.TAINTED else None


def _joined(node: ast.Call, chains: DefUse | None, seen: set[str]) -> ast.expr | None:
    """What ``os.path.join(base, part)`` puts into its result.

    Looked inside rather than declined, because it is not opaque the way a
    project's own helper is: an absolute argument becomes the whole answer and
    a relative one is appended unchanged.
    """
    function = node.func
    if not isinstance(function, ast.Attribute) or function.attr not in JOINS:
        return None
    holder = function.value
    name = (
        holder.attr
        if isinstance(holder, ast.Attribute)
        else holder.id
        if isinstance(holder, ast.Name)
        else None
    )
    if name not in JOIN_HOLDERS:
        return None
    return _any_part(tuple(node.args), chains, seen)


def _any_part(
    parts: tuple[ast.expr | ast.FormattedValue, ...], chains: DefUse | None, seen: set[str]
) -> ast.expr | None:
    """The first part the request supplies. Position carries no meaning here."""
    for part in parts:
        inner = part.value if isinstance(part, ast.FormattedValue) else part
        if isinstance(inner, ast.Constant):
            continue
        found = traversable(inner, chains, seen)
        if found is not None:
            return found
    return None


@register
class PathTraversal(Rule):
    """`DJI-012` -- the request chooses which file is opened."""

    meta = RuleMeta(
        id="DJI-012",
        title="File path chosen by request data",
        family=Family.DJI,
        severity=Severity.HIGH,
        confidence=Confidence.CERTAIN,
        tier=Tier.STATIC,
        rationale=(
            "A path assembled from request data lets the client name a file "
            "the view never meant to expose. A leading slash makes os.path.join "
            "discard the base directory outright, and a ../ climbs out of it "
            "after normalisation, so a constant prefix is not a defence. Read "
            "access leaks settings modules, private keys and the database file; "
            "the same path handed to os.remove or shutil.move deletes or "
            "relocates them."
        ),
        remediation=(
            "Do not build a path from request data. Take a key from the request "
            "and look the path up, or store the file through Django's storage "
            "API, whose FileSystemStorage refuses a traversing name. Where a "
            "path must be assembled, django.utils._os.safe_join raises rather "
            "than escaping its base."
        ),
        references=(
            "https://cwe.mitre.org/data/definitions/22.html",
            "https://docs.djangoproject.com/en/stable/ref/files/storage/",
            "https://docs.python.org/3/library/os.path.html#os.path.join",
            "https://owasp.org/www-community/attacks/Path_Traversal",
        ),
        limitations=(
            "A path is reported only when taint analysis proves it came from "
            "the request. A filename read from a model field is not reported, "
            "though a stored path is a real way to traverse.",
            "Reads through Django's storage API are never reported, because "
            "FileSystemStorage resolves names with safe_join and raises "
            "SuspiciousFileOperation on a traversing one.",
            "A call the project wrote is never treated as the request being "
            "read, so a helper returning an unfiltered path is missed. This is "
            "the trade that lets basename and safe_join go unlisted.",
            "Only os and shutil are read as filesystem modules. A path handed "
            "to a third-party library that opens it is outside what this rule "
            "can follow from the call alone.",
        ),
    )

    def admits(self, tree: ast.AST) -> bool:
        """Whether this file touches the filesystem at all."""
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for _ in accesses(node):
                    return True
        return False

    def check(self, ctx: ProjectContext) -> Iterator[Finding]:
        for path in ctx.python_files:
            source = ctx.source(path)
            if source is None or not any(word in source for word in WORDS):
                continue
            tree = ctx.parse(path)
            if tree is None or not self.admits(tree):
                continue
            root = ctx.scopes(path)
            if root is None:
                continue
            yield from self.inspect(ctx, path, root)

    def inspect(self, ctx: ProjectContext, path: FsPath, scope: Scope) -> Iterator[Finding]:
        here = [
            found
            for node in own_nodes(scope)
            if isinstance(node, ast.Call)
            for found in accesses(node)
        ]
        if here:
            chains = def_use(scope)
            for found in here:
                supplied = traversable(found.path, chains)
                if supplied is not None:
                    yield self.report(ctx, path, found, supplied)
        for child in scope.children:
            yield from self.inspect(ctx, path, child)

    def report(
        self, ctx: ProjectContext, path: FsPath, found: Access, supplied: ast.expr
    ) -> Finding:
        """Report one access. Always ``certain``, for `DJI-009`'s reason.

        :func:`traversable` resolves a name to its definition before deciding
        and declines anything it cannot resolve, so every finding has a request
        source to name and a lower-confidence branch would be unreachable.
        """
        written = ast.unparse(supplied)
        source = _source_of(supplied)
        return self.finding(
            location=ctx.location(path, found.call),
            confidence=Confidence.CERTAIN,
            message=(
                f"{found.name}() is given a path that {source or written} reaches, "
                f"so the client chooses which file is touched"
            ),
            evidence=(
                Evidence(
                    kind=EvidenceKind.AST,
                    content=ast.unparse(found.call),
                    source=f"{path}:{found.call.lineno}",
                ),
                Evidence(
                    kind=EvidenceKind.AST,
                    content=written,
                    source=f"{path}:{getattr(supplied, 'lineno', found.call.lineno)}",
                ),
            ),
            properties={"sink": found.name},
        )


def _source_of(node: ast.expr) -> str | None:
    for inner in ast.walk(node):
        if isinstance(inner, ast.expr):
            found = request_source(inner)
            if found is not None:
                return found
    return None

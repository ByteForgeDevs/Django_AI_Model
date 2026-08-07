"""`DJI-011` -- request data marked as trusted HTML.

Three measurements shaped this rule, and the first reverses the way the shape
is usually flagged.

**``format_html`` escapes its arguments and not its format string.** Reading
Django's source, ``format_html`` maps ``conditional_escape`` over ``*args`` and
``**kwargs`` and then calls ``format_string.format(...)`` on a string it never
touches. Confirmed by construction against Django 6.0::

    format_html("<b>{}</b>", payload)  -> '<b>&lt;img src=x ...&gt;</b>'
    format_html(payload + "{}", 1)     -> '<img src=x onerror=alert(1)>1'
    format_html(payload)               -> TypeError: args or kwargs must be
                                          provided.

So the argument -- the thing a name-keyed lint reports -- is the one position
that is safe, and the format string is the sink. The third line is why a
``format_html`` call carrying no arguments is not reported: on every supported
Django it raises before rendering anything, which makes it a crash rather than
a vulnerability.

**Every part of a marked-safe string is a sink.** This is where the rule
departs from `DJI-010`, which reads only the *leading* part of a redirect
target because a ``Location`` header is decided from its first character. HTML
has no such privilege: ``"<b>" + tainted`` and ``tainted + "</b>"`` inject
equally well, so this rule asks about every part and stops at the first one the
request supplies.

**All three benchmark hits are correct code, and two of them are already
escaped.** A naive rule -- any of the 233 sink calls whose arguments are
tainted -- reports three sites across the corpus::

    mark_safe(_('Added member <a href="{url}">{device}</a>').format(
        url=device.get_absolute_url(), device=escape(device)))     # netbox
    mark_safe(_('Subject: {subject}').format(
        subject=prefix_subject(event, escape(subject), ...)))      # pretix
    mark_safe(html)   # netbox, where html is assembled from f-strings

Every one is `DJI-010`'s finding again: a call inherits taint from its
arguments, so anything a project computes from request data comes back
``TAINTED``. :func:`~djaudit.rules._injection.reads_request` is what separates
them, and it is why this rule needs no list of sanitiser names -- ``escape(x)``
is declined for the same reason ``prefix_subject(...)`` is, that neither is the
request being read. A name-keyed sanitiser list would have been unreachable
code sitting behind that test.

``str.format`` is handled rather than declined, because unlike ``reverse()`` it
is not opaque: its arguments are spliced into the result verbatim, so
``mark_safe("<b>{}</b>".format(request.GET["x"]))`` is a real finding and the
rule says so.
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
    from pathlib import Path

    from djaudit.context import ProjectContext
    from djaudit.dataflow.chains import DefUse
    from djaudit.dataflow.scopes import Scope

TRUSTED = {"mark_safe": 0, "SafeString": 0, "SafeText": 0, "format_html_join": 1}
"""Callables that promise the rendered page will not escape one argument.

The value is which argument carries the promise. ``format_html_join(sep, fmt,
rows)`` passes ``sep`` through ``conditional_escape`` and its rows through
``format_html``, leaving the format string at index 1 as the only unescaped
position.
"""

FORMAT_HTML = "format_html"
"""Handled apart from :data:`TRUSTED` because its arity decides the question."""

SPLICES = frozenset({"format", "format_map"})
"""Methods that put their arguments into a string without escaping them."""

WORDS = ("mark_safe", "format_html", "SafeString", "SafeText")
"""A necessary condition on a file's text: no sink can be spelled without one."""


@dataclass(frozen=True, slots=True)
class Trusted:
    """One call marking a value as HTML, and the value it marks."""

    call: ast.Call
    value: ast.expr
    name: str


def marked(node: ast.Call) -> Iterator[Trusted]:
    """The safe-marking this call performs, if it performs one."""
    function = node.func
    if isinstance(function, ast.Attribute):
        name = function.attr
    elif isinstance(function, ast.Name):
        name = function.id
    else:
        return

    if name == FORMAT_HTML:
        # Django raises TypeError when neither args nor kwargs are given, so a
        # lone format string cannot reach a template however tainted it is.
        if len(node.args) >= 2 or (node.args and node.keywords):
            yield Trusted(node, node.args[0], name)
        return

    index = TRUSTED.get(name)
    if index is None or len(node.args) <= index:
        return
    yield Trusted(node, node.args[index], name)


def unescaped(
    node: ast.expr, chains: DefUse | None, seen: set[str] | None = None
) -> ast.expr | None:
    """The part of *node* the request supplies, if any part of it does.

    Every part is asked, unlike `DJI-010`'s leading-part walk, because a payload
    injects from anywhere in a fragment of HTML.
    """
    if seen is None:
        seen = set()

    if isinstance(node, ast.Call):
        if reads_request(node, chains):
            return node
        return _spliced(node, chains, seen)

    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _any_part((node.left, node.right), chains, seen)

    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        # "<b>%s</b>" % value -- both sides reach the page unescaped.
        return _any_part((node.left, node.right), chains, seen)

    if isinstance(node, ast.Tuple | ast.List):
        return _any_part(tuple(node.elts), chains, seen)

    if isinstance(node, ast.JoinedStr):
        return _any_part(tuple(node.values), chains, seen)

    if isinstance(node, ast.Name) and chains is not None:
        # A name already on this walk has been examined; saying so is not the
        # same as falling through. netbox assembles a table cell across five
        # assignments ending in mark_safe(html), and html reaches button twice.
        # The second visit used to drop to the taint fallback below, which said
        # TAINTED -- true, but about a value that had already been declined
        # because quote() percent-encodes it. A recursion guard that gives up
        # into a permissive default reverses the decision it was protecting.
        if node.id in seen:
            return None
        seen.add(node.id)
        for binding in chains.reaching(node):
            if binding.value is None:
                continue
            found = unescaped(binding.value, chains, seen)
            if found is not None:
                return found
        return None

    return node if taint_of(node, chains) is Taint.TAINTED else None


def _spliced(node: ast.Call, chains: DefUse | None, seen: set[str]) -> ast.expr | None:
    """What ``"...".format(x)`` puts into its result.

    ``str.format`` is the one call this rule looks inside. It is not opaque the
    way a project's own helper is: whatever is handed to it appears in the
    output with nothing done to it.
    """
    if not isinstance(node.func, ast.Attribute) or node.func.attr not in SPLICES:
        return None
    parts = (*node.args, *(keyword.value for keyword in node.keywords))
    return _any_part(parts, chains, seen)


def _any_part(
    parts: tuple[ast.expr | ast.FormattedValue, ...], chains: DefUse | None, seen: set[str]
) -> ast.expr | None:
    """The first part the request supplies. Position carries no meaning here."""
    for part in parts:
        inner = part.value if isinstance(part, ast.FormattedValue) else part
        if isinstance(inner, ast.Constant):
            continue
        found = unescaped(inner, chains, seen)
        if found is not None:
            return found
    return None


@register
class TrustedHtml(Rule):
    """`DJI-011` -- request data marked as HTML that will not be escaped."""

    meta = RuleMeta(
        id="DJI-011",
        title="Request data marked as trusted HTML",
        family=Family.DJI,
        severity=Severity.HIGH,
        confidence=Confidence.CERTAIN,
        tier=Tier.STATIC,
        rationale=(
            "Django escapes template variables by default, and mark_safe is how "
            "that default is switched off for one value. Handing it request "
            "data turns the escaping off for exactly the string an attacker "
            "controls, which is cross-site scripting in its plainest form. "
            "format_html is the safe alternative only when the untrusted part "
            "is one of its arguments: its format string is never escaped, so "
            "building that string out of request data reopens the same hole "
            "through the function meant to close it."
        ),
        remediation=(
            "Let the template escape it. Where a fragment really must be built "
            "in Python, pass the untrusted value as a format_html argument "
            "rather than splicing it into the format string, and reserve "
            "mark_safe for markup the project wrote itself."
        ),
        references=(
            "https://cwe.mitre.org/data/definitions/79.html",
            "https://docs.djangoproject.com/en/stable/ref/utils/#django.utils.html.format_html",
            "https://docs.djangoproject.com/en/stable/topics/templates/#automatic-html-escaping",
            "https://docs.djangoproject.com/en/stable/ref/utils/#django.utils.safestring.mark_safe",
        ),
        limitations=(
            "A value is reported only when taint analysis proves it came from "
            "the request. HTML built from a model field is not reported, "
            "though stored cross-site scripting is a real attack.",
            "A call the project wrote is never treated as the request being "
            "read, so a helper that returns request data unescaped is missed. "
            "This is the same trade that keeps the rule silent on three "
            "correct benchmark sites whose taint is real.",
            "Templates are not parsed, so the safe filter and an autoescape "
            "off block are outside what this rule can see. It reads Python "
            "source only, which is where mark_safe is written.",
            "A format_html call given no arguments is not reported, because "
            "Django raises TypeError before rendering anything and the call is "
            "therefore a crash rather than a way to reach the page.",
        ),
    )

    def admits(self, tree: ast.AST) -> bool:
        """Whether this file marks anything as trusted HTML."""
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for _ in marked(node):
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

    def inspect(self, ctx: ProjectContext, path: Path, scope: Scope) -> Iterator[Finding]:
        here = [
            found
            for node in own_nodes(scope)
            if isinstance(node, ast.Call)
            for found in marked(node)
        ]
        if here:
            chains = def_use(scope)
            for found in here:
                value = unescaped(found.value, chains)
                if value is not None:
                    yield self.report(ctx, path, found, value)
        for child in scope.children:
            yield from self.inspect(ctx, path, child)

    def report(self, ctx: ProjectContext, path: Path, found: Trusted, value: ast.expr) -> Finding:
        """Report one marking. Always ``certain``, for `DJI-009`'s reason.

        :func:`unescaped` resolves a name to its definition before deciding and
        declines anything it cannot resolve, so every finding has a request
        source to name and a lower-confidence branch would be unreachable.
        """
        written = ast.unparse(value)
        source = _source_of(value)
        position = (
            "format string" if found.name in {FORMAT_HTML, "format_html_join"} else "argument"
        )
        return self.finding(
            location=ctx.location(path, found.call),
            confidence=Confidence.CERTAIN,
            message=(
                f"{found.name}() is given a {position} that {source or written} reaches, "
                f"so request data is written into the page with its markup intact"
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
                    source=f"{path}:{getattr(value, 'lineno', found.call.lineno)}",
                ),
            ),
            properties={"sink": found.name, "position": position},
        )


def _source_of(node: ast.expr) -> str | None:
    for inner in ast.walk(node):
        if isinstance(inner, ast.expr):
            found = request_source(inner)
            if found is not None:
                return found
    return None

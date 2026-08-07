"""`DJI-009` -- request data choosing the host of an outbound request.

Two measurements shaped this rule, and both contradict the way SSRF is usually
linted.

**A name-keyed matcher is unusable here.** Counting what
``{client,session}.{get,post}`` would match across the three benchmarks found
over 4,400 sites and not one of them an HTTP request: ``self.client.get(...)``,
``token_client.post(...)`` and ``device_client.get(...)`` are Django's *test
client*, while ``request.session.get("last_project_id")`` and
``self.cart_session.get("widget_data", {})`` are dictionary lookups. A rule
keyed on the receiver's name would report thousands of findings on exactly the
shapes it must not. So the holder is read as a module name and nothing else,
the same discipline `DJI-007` and `DJI-008` use, which leaves 14 non-constant
URLs across 3,091 files.

**``urljoin`` is a sink and concatenation is not.** Measured against
``https://api.internal.example.com/v1/``:

| payload | ``urljoin(BASE, p)`` | ``BASE + p`` |
|---|---|---|
| ``users/1`` | api.internal.example.com | api.internal.example.com |
| ``http://evil.com/x`` | **evil.com** | api.internal.example.com |
| ``//evil.com/x`` | **evil.com** | api.internal.example.com |
| ``../../../secret`` | api.internal.example.com | api.internal.example.com |

``urljoin`` is specified to let an absolute or protocol-relative reference
replace the base, so a constant base is no defence whatever. Plain
concatenation drops the payload into the path, where it cannot move the host.
The conventional rule -- flag string building, trust the URL helper -- reports
the safe shape and misses the dangerous one.

**And it is symmetric.** Measuring the other argument, which the first pass of
this rule did not, found a case it was silently missing::

    urljoin(TAINTED, "/health")            -> the request's host
    urljoin(TAINTED, "http://good/x")      -> good
    urljoin(TAINTED, "//good/x")           -> good

A reference that names a host of its own replaces the base; one that does not
leaves the base choosing. Which argument matters depends on the other, so both
are asked and neither is assumed.

What this rule therefore asks is not "was the URL built from a request" but
**"can the request choose the authority"**. A tainted path on a fixed host is
somebody else's finding: it cannot reach an internal service, and reporting it
here is how a family loses its precision record.
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
from djaudit.rules._injection import own_calls

if TYPE_CHECKING:
    from pathlib import Path

    from djaudit.context import ProjectContext
    from djaudit.dataflow.chains import DefUse
    from djaudit.dataflow.scopes import Scope

FETCHERS = {
    "requests": frozenset({"get", "post", "put", "patch", "delete", "head", "options", "request"}),
    "httpx": frozenset({"get", "post", "put", "patch", "delete", "head", "options", "stream"}),
    "urllib3": frozenset({"request"}),
}
"""Modules whose functions take a URL first and fetch it.

Keyed on the module rather than the verb, because the verbs alone are the names
Django's test client and every dictionary in the language also use.
"""

OPENERS = frozenset({"urlopen", "urlretrieve"})
"""``urllib.request`` entry points, matched on the function name.

These are unambiguous in a way ``get`` is not: nothing else in a Django project
is called ``urlopen``, so the holder is not required to be spelled a fixed way.
"""

URL_ARGUMENT = {"requests.request": 1, "urllib3.request": 1}
"""Entry points whose first argument is the method, so the URL is second."""

JOINERS = frozenset({"urljoin"})
"""Helpers measured to let a reference replace the base's authority."""

WORDS = ("requests", "httpx", "urllib", "urlopen", "urlretrieve")
"""A necessary condition on a file's text: no sink can be spelled without one."""

SOURCE_WORDS = ("request", "self.kwargs")
"""The taint model's own necessary condition, reused as a prefilter.

``request_source`` recognises an attribute of the name ``request`` and the
literal ``self.kwargs``, and taint does not cross a scope boundary, so a file
holding neither string can never produce a tainted argument.
"""


@dataclass(frozen=True, slots=True)
class Fetch:
    """One outbound request, and the expression that supplies its URL."""

    call: ast.Call
    url: ast.expr
    name: str


def _module(node: ast.Attribute) -> str | None:
    """The immediate holder of an attribute, when it is a plain name.

    Deliberately shallow. ``self.client.get`` and ``request.session.get`` both
    have an attribute holder, and reading through it is what would turn a test
    suite and a dictionary into thousands of SSRF findings.
    """
    holder = node.value
    return holder.id if isinstance(holder, ast.Name) else None


def _keyword(node: ast.Call, name: str) -> ast.expr | None:
    for keyword in node.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def fetches(node: ast.Call) -> Iterator[Fetch]:
    """The outbound request this call makes, if it makes one.

    Shared by the file prefilter and the body so the two cannot disagree about
    what counts as a sink.
    """
    if isinstance(node.func, ast.Name):
        # `from urllib.request import urlopen` is the ordinary spelling, and
        # nothing else in a Django project is called urlopen, so this one name
        # is safe to match unqualified in a way `get` is not.
        if node.func.id not in OPENERS:
            return
        function, module = node.func.id, None
    elif isinstance(node.func, ast.Attribute):
        function, module = node.func.attr, _module(node.func)
    else:
        return
    if function in OPENERS:
        name = f"{module}.{function}" if module else function
    elif module in FETCHERS and function in FETCHERS[module]:
        name = f"{module}.{function}"
    else:
        return

    index = URL_ARGUMENT.get(name, 0)
    url = node.args[index] if len(node.args) > index else _keyword(node, "url")
    if url is not None:
        yield Fetch(node, url, name)


def authority(node: ast.expr, chains: DefUse | None) -> ast.expr | None:
    """The tainted part of *node* that can choose the host, if there is one.

    Returns ``None`` when the request can only influence the path. That is the
    distinction the measurement forced: a payload appended after a ``/`` cannot
    move the authority, while one that replaces the reference outright can.
    """
    return _authority(node, chains, set())


def _authority(node: ast.expr, chains: DefUse | None, seen: set[str]) -> ast.expr | None:
    if isinstance(node, ast.Call):
        joined = _joined(node)
        if joined is not None:
            return _resolved(joined, chains, seen)
        return node if taint_of(node, chains) is Taint.TAINTED else None

    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _concatenated((node.left, node.right), chains, seen)

    if isinstance(node, ast.JoinedStr):
        return _concatenated(tuple(node.values), chains, seen)

    if isinstance(node, ast.Name) and chains is not None and node.id not in seen:
        seen.add(node.id)
        for binding in chains.reaching(node):
            if binding.value is None:
                continue
            found = _authority(binding.value, chains, seen)
            if found is not None:
                return found
        return None

    return node if taint_of(node, chains) is Taint.TAINTED else None


def _concatenated(
    parts: tuple[ast.expr | ast.FormattedValue, ...], chains: DefUse | None, seen: set[str]
) -> ast.expr | None:
    """The first tainted part that is still inside the authority.

    Everything before a ``/`` that ends the authority is host; everything after
    it is path. Walking left to right and stopping at that ``/`` is what tells
    ``f"https://{host}/x"`` apart from ``BASE + path``.
    """
    for part in parts:
        if isinstance(part, ast.FormattedValue):
            interpolated = part.value
        else:
            text = _literal(part)
            if text is not None:
                if _ends_authority(text):
                    return None
                continue
            interpolated = part
        found = _authority(interpolated, chains, seen)
        if found is not None:
            return found
        # A part that is neither a literal nor tainted is a URL fragment this
        # rule cannot read. It may well have ended the authority -- a module
        # level BASE almost always has -- so nothing after it can be claimed to
        # choose the host.
        return None
    return None


def _literal(node: ast.expr) -> str | None:
    """The constant string this part contributes, or ``None`` if it is dynamic."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _ends_authority(text: str) -> bool:
    """Whether a constant reaches past the host into the path.

    ``https://host/`` does; ``https://`` does not. The scheme's own ``//`` is
    skipped before looking, so it is not mistaken for the path separator.
    """
    rest = text.split("://", 1)[1] if "://" in text else text
    if text.startswith("//"):
        rest = text[2:]
    return "/" in rest or "?" in rest or "#" in rest


def _joined(node: ast.Call) -> tuple[ast.expr, ast.expr] | None:
    """The base and reference arguments of a ``urljoin`` call, or ``None``."""
    tail = (
        node.func.attr
        if isinstance(node.func, ast.Attribute)
        else (node.func.id if isinstance(node.func, ast.Name) else None)
    )
    if tail not in JOINERS or len(node.args) < 2:
        return None
    return node.args[0], node.args[1]


def _resolved(
    joined: tuple[ast.expr, ast.expr], chains: DefUse | None, seen: set[str]
) -> ast.expr | None:
    """Which argument of a ``urljoin`` chooses the host.

    Measured against CPython's own ``urljoin``, both ways round::

        urljoin(BASE, "/health")          -> BASE's host
        urljoin(BASE, "//evil.com/x")     -> evil.com
        urljoin(BASE, "http://evil.com")  -> evil.com
        urljoin(EVIL, "/health")          -> EVIL's host
        urljoin(EVIL, "http://good/x")    -> good

    So a reference that names a host of its own replaces the base entirely, and
    one that does not leaves the base choosing. Both arguments are therefore
    worth asking about, and which one answers depends on the other.
    """
    base, reference = joined
    found = _authority(reference, chains, seen)
    if found is not None:
        return found
    text = _literal(reference)
    if text is None or _names_a_host(text):
        # Either the reference pins the host, or it is an expression this rule
        # cannot read -- in which case it may pin the host and the base cannot
        # be blamed for a choice it might not be making.
        return None
    return _authority(base, chains, seen)


def _names_a_host(text: str) -> bool:
    """Whether a constant reference carries an authority of its own."""
    return "://" in text or text.startswith("//")


@register
class ServerSideRequestForgery(Rule):
    """`DJI-009` -- a request choosing the host of an outbound request."""

    meta = RuleMeta(
        id="DJI-009",
        title="Request data chooses the host of an outbound request",
        family=Family.DJI,
        severity=Severity.HIGH,
        confidence=Confidence.CERTAIN,
        tier=Tier.STATIC,
        rationale=(
            "An application server usually sits somewhere a client does not: "
            "inside the VPC, next to the metadata service, able to reach the "
            "database's admin port and every internal API that trusts the network "
            "instead of a token. When a request chooses the host, it borrows that "
            "position, and the response often comes back in the page. This is why "
            "SSRF is judged on the authority rather than on whether a URL was "
            "built from user input at all: a tainted path on a fixed host stays "
            "inside a service that was already reachable."
        ),
        remediation=(
            "Do not let a request supply a host. Where the target genuinely "
            "varies, resolve it from a fixed mapping rather than from the value, "
            "so the client selects a key and the server selects the URL. If an "
            "arbitrary URL is unavoidable, parse it, require the scheme to be "
            "http or https, resolve the name and reject addresses that are "
            "private, loopback or link-local -- and re-check after every redirect, "
            "because the first response can send the client somewhere else."
        ),
        references=(
            "https://cwe.mitre.org/data/definitions/918.html",
            "https://owasp.org/www-community/attacks/Server_Side_Request_Forgery",
            "https://docs.python.org/3/library/urllib.parse.html#urllib.parse.urljoin",
            "https://requests.readthedocs.io/en/latest/user/advanced/",
        ),
        limitations=(
            "A URL is reported only when taint analysis proves it came from the "
            "request. A host read from a model field, a setting or a helper's "
            "parameter is unknown rather than tainted and is not reported.",
            "Only the authority is reported. A request value appended after a "
            "slash was measured unable to move the host, so it is left to the path "
            "traversal rule rather than reported as request forgery here.",
            "The fetching module must be named at the call, as requests.get or "
            "httpx.post. A session object held on self is not recognised, because "
            "matching receivers by name was measured to select over 4,400 Django "
            "test client calls and dictionary reads across the benchmarks.",
            "Redirects are not followed. A constant URL that redirects to a host "
            "the request chose is a real defect this rule cannot see, since it "
            "would need the response rather than the source.",
            "A part of the URL that cannot be read, such as a base imported from "
            "settings, ends the reasoning. It is far more often a whole base URL "
            "than a bare scheme, so what follows it is treated as path and a host "
            "appended to an unresolvable prefix is not reported.",
        ),
    )

    def admits(self, tree: ast.AST) -> bool:
        """Whether this file holds a fetch worth building a scope tree for."""
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for _ in fetches(node):
                return True
        return False

    def check(self, ctx: ProjectContext) -> Iterator[Finding]:
        for path in ctx.python_files:
            source = ctx.source(path)
            if source is None or not any(word in source for word in WORDS):
                continue
            if not any(word in source for word in SOURCE_WORDS):
                continue
            tree = ctx.parse(path)
            if tree is None or not self.admits(tree):
                continue
            root = ctx.scopes(path)
            if root is None:
                continue
            yield from self.inspect(ctx, path, root)

    def inspect(self, ctx: ProjectContext, path: Path, scope: Scope) -> Iterator[Finding]:
        """Walk one scope's own calls, then its children.

        Def-use chains are built only for scopes that hold a syntactic match,
        because they cost far more than the test that decides whether they are
        wanted.
        """
        here = [found for node in own_calls(scope) for found in fetches(node)]
        if here:
            chains = def_use(scope)
            for found in here:
                host = authority(found.url, chains)
                if host is not None:
                    yield self.report(ctx, path, found, host)
        for child in scope.children:
            yield from self.inspect(ctx, path, child)

    def report(self, ctx: ProjectContext, path: Path, found: Fetch, host: ast.expr) -> Finding:
        """Report one fetch. Always ``certain``, and that is a measured claim.

        :func:`authority` reports the *resolved* origin rather than the
        expression written at the call, following a name to its definition
        before deciding, and it declines any name it cannot resolve. Every shape
        that reaches here therefore has a request source to name -- which is why
        there is no lower-confidence branch, rather than a branch nothing can
        reach.
        """
        written = ast.unparse(host)
        source = _source_of(host)
        arrival = f"{source} reaches it" if source else f"{written} carries request data"
        return self.finding(
            location=ctx.location(path, found.call),
            confidence=Confidence.CERTAIN,
            message=(
                f"{found.name} fetches a URL whose host comes from {written}, and "
                f"{arrival}. A client that controls the host points this server at "
                f"a machine of its choosing, which is request forgery rather than a "
                f"bad path."
            ),
            evidence=(
                Evidence(
                    kind=EvidenceKind.AST,
                    content=ast.unparse(found.call),
                    source="the fetch and the expression supplying its host",
                ),
            ),
            properties={
                "sink": found.name,
                "url": ast.unparse(found.url),
                "authority": written,
                "source": source or "unknown",
            },
        )


def _source_of(node: ast.expr) -> str | None:
    """The request attribute somewhere inside this expression.

    :func:`request_source` resolves a plain attribute chain, so it answers for
    ``request.body`` and returns ``None`` for ``request.GET["url"]``, which is
    the shape actually written at a call.
    """
    for inner in ast.walk(node):
        if isinstance(inner, ast.expr):
            found = request_source(inner)
            if found is not None:
                return found
    return None

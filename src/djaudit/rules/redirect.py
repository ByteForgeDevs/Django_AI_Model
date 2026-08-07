"""`DJI-010` -- a redirect sending the client to a host it chose itself.

Three measurements shaped this rule, and the third overturned the design.

**Django already blocks the scheme, and only the scheme.** Reading
``HttpResponseRedirectBase``, ``allowed_schemes`` is ``http``, ``https`` and
``ftp``, and anything else raises ``DisallowedRedirect``. Confirmed by
construction::

    HttpResponseRedirect("javascript:alert(1)")  -> DisallowedRedirect
    HttpResponseRedirect("//evil.com")           -> sent
    HttpResponseRedirect("http://evil.com")      -> sent
    HttpResponseRedirect("https:evil.com")       -> sent

So the ``javascript:`` payload every open-redirect article opens with is not a
finding on Django at all, and the host takeover -- the thing that actually
phishes a session -- is passed through untouched. This rule asks about the host.

**A route name is not a URL, but a URL still gets through.** ``redirect()``
defers to ``resolve_url()``, whose source reverses its argument as a view name
and, on ``NoReverseMatch``, re-raises unless the string contains a ``/`` or a
``.``. Both are present in every payload that matters, so the fallback is not a
narrow escape hatch: any absolute URL reaches the ``Location`` header.

**Every benchmark guards its redirects, and no two do it the same way.** This
is what the rule had to be built around. Across 472 redirect calls:

| project | how the target is checked |
|---|---|
| pretix | ``url_has_allowed_host_and_scheme`` directly, 41 times |
| netbox | ``safe_for_redirect()``, a local wrapper around that function |
| healthchecks | ``_allow_redirect()``, hand-rolled on ``urlparse().netloc`` |

A rule that recognised Django's function by name would have been correct on one
project and wrong on two, reporting careful defensive code as a vulnerability.
So a validator is *resolved* rather than named: a project-local function counts
as one when its body calls a known validator, or when it inspects the authority
of a parsed URL. The set is built once per run and then used exactly as
`DJI-006` uses its allowlist -- matched against the value's identity rather than
its spelling, because the guard is written against ``request.GET["next"]`` and
the redirect is written against the local that holds it.

What the guarding branch then *does* is not examined, for the reason `DJI-006`
gives: deciding whether every path out of a check either rejects the request or
substitutes a default is how a rule starts calling correct code a defect.
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
from djaudit.rules._injection import (
    identity,
    names_read,
    own_nodes,
    parameters_read,
    reads_request,
)

if TYPE_CHECKING:
    from pathlib import Path

    from djaudit.context import ProjectContext
    from djaudit.dataflow.chains import DefUse
    from djaudit.dataflow.scopes import Scope

REDIRECTS = frozenset(
    {
        "redirect",
        "HttpResponseRedirect",
        "HttpResponsePermanentRedirect",
        "redirect_to_login",
    }
)
"""Callables that put their first argument in a ``Location`` header.

Matched on the function name. Unlike `DJI-009`'s ``get``, none of these is a
word another object is likely to answer to, and the corpus bears that out: all
472 matches across the three benchmarks were redirects.
"""

VALIDATORS = frozenset({"url_has_allowed_host_and_scheme", "is_safe_url"})
"""Django's own answer to this question, under both its names.

``is_safe_url`` was renamed in Django 3.0 and projects still carry wrappers
under the old name, so both are recognised.
"""

PARSERS = frozenset({"urlparse", "urlsplit"})
"""What a hand-rolled validator uses to reach the authority."""

AUTHORITY = frozenset({"netloc", "hostname"})
"""Reading either of these off a parsed URL is checking *where* it points.

``scheme`` is deliberately absent. Django already rejects an unusual scheme, so
a function that checks only that one is not deciding the question this rule
asks, and honouring it as a guard would excuse a redirect to any host at all.
"""

WORDS = ("redirect", "Redirect")
"""A necessary condition on a file's text: no sink can be spelled without one."""


@dataclass(frozen=True, slots=True)
class Redirect:
    """One redirect, and the expression that supplies its target."""

    call: ast.Call
    target: ast.expr
    name: str


def redirects(node: ast.Call) -> Iterator[Redirect]:
    """The redirect this call performs, if it performs one."""
    function = node.func
    if isinstance(function, ast.Attribute):
        name = function.attr
    elif isinstance(function, ast.Name):
        name = function.id
    else:
        return
    if name not in REDIRECTS:
        return
    if not node.args:
        return
    # Extra arguments are not a reason to look away. redirect("view", pk=1)
    # reverses a route, but its first argument is a constant and so declines
    # itself, while redirect(url, permanent=True) is a redirect like any other
    # -- an earlier draft skipped both and would have missed the second.
    yield Redirect(node, node.args[0], name)


def validates(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether this function decides where a URL points.

    Two shapes, both taken from the benchmarks. netbox forwards to Django::

        def safe_for_redirect(url):
            return url_has_allowed_host_and_scheme(url, allowed_hosts=None)

    and healthchecks does the work itself::

        def _allow_redirect(redirect_url):
            parsed = urlparse(redirect_url)
            if parsed.netloc:
                return False

    Reading the authority off a parsed URL is the operation they have in common,
    and it is not one a function performs for any other reason.
    """
    parsed = authority = False
    for node in ast.walk(function):
        if isinstance(node, ast.Call) and _called(node) in VALIDATORS:
            return True
        if isinstance(node, ast.Call) and _called(node) in PARSERS:
            parsed = True
        if isinstance(node, ast.Attribute) and node.attr in AUTHORITY:
            authority = True
    return parsed and authority


def _called(node: ast.Call) -> str | None:
    function = node.func
    if isinstance(function, ast.Attribute):
        return function.attr
    return function.id if isinstance(function, ast.Name) else None


def chosen(node: ast.expr, chains: DefUse | None, seen: set[str] | None = None) -> ast.expr | None:
    """The part of *node* the request can actually choose, if there is one.

    Taint alone is not the question, and the benchmarks are what proved it.
    Three pretix redirects are ``TAINTED`` and all three are correct code::

        redirect(reverse("control:users.edit", kwargs=self.kwargs))
        redirect(self.get_process_url(request, cf, charset))
        HttpResponseRedirect(signing.Signer(salt=salt).unsign(request.GET["url"]))

    A call takes taint from its arguments, so anything a project builds out of
    request data comes back tainted -- but ``reverse()`` resolves against the
    urlconf, a project's own URL builder does the same, and ``unsign()`` is a
    signature check that fails closed. None of them can be made to name another
    host. So a call is evidence only when it is the request being *read*, as
    ``request.GET.get("next")`` is, and never when it is the project computing
    something.
    """
    if seen is None:
        seen = set()

    if isinstance(node, ast.Call):
        return node if reads_request(node, chains) else None

    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _leads((node.left, node.right), chains, seen)

    if isinstance(node, ast.JoinedStr):
        return _leads(tuple(node.values), chains, seen)

    if isinstance(node, ast.Name) and chains is not None and node.id not in seen:
        seen.add(node.id)
        for binding in chains.reaching(node):
            if binding.value is None:
                continue
            found = chosen(binding.value, chains, seen)
            if found is not None:
                return found
        return None

    return node if taint_of(node, chains) is Taint.TAINTED else None


def _leads(
    parts: tuple[ast.expr | ast.FormattedValue, ...], chains: DefUse | None, seen: set[str]
) -> ast.expr | None:
    """Only the first part of a redirect target decides where it points.

    Unlike an outbound URL, which has an authority somewhere in the middle, a
    ``Location`` is read from its first character: anything after a leading
    ``/dashboard`` is path, and anything after a leading ``https://host`` is
    path too. The one exception is a constant that stops inside the authority,
    which is what ``"//" + host`` and ``"https://" + host`` both do.
    """
    for part in parts:
        if isinstance(part, ast.FormattedValue):
            return chosen(part.value, chains, seen)
        text = _literal(part)
        if text is None:
            return chosen(part, chains, seen)
        if _pins(text):
            return None
        # The constant stops inside the authority, so whatever follows supplies
        # the host. Keep walking rather than asking about the constant itself.
    return None


def _literal(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _pins(text: str) -> bool:
    """Whether a leading constant settles where the target points."""
    if not text:
        return False
    if text.startswith("//"):
        # "//" alone hands the host straight to whatever follows it.
        return len(text) > 2
    if "://" in text:
        return not text.endswith("://")
    return True


def guarded(
    target: ast.expr, scope: Scope, chains: DefUse | None, validators: frozenset[str]
) -> bool:
    """Whether this scope checks where the target points before using it.

    The guard has to be about *this* value, which is what
    :func:`~djaudit.rules._injection.identity` establishes: the redirect is
    written against a local while the check is written against the request read
    it came from, and following reaching definitions is what connects the two.
    Matching on syntax instead would let ``if request.method == "POST"`` excuse
    every redirect in the same view.
    """
    names, parameters = identity(target, chains)
    if not names and not parameters:
        return False
    for node in own_nodes(scope):
        if not isinstance(node, ast.Call) or _called(node) not in validators:
            continue
        for argument in [*node.args, *(keyword.value for keyword in node.keywords)]:
            if names_read(argument) & names or parameters_read(argument) & parameters:
                return True
    return False


@register
class OpenRedirect(Rule):
    """`DJI-010` -- the client chooses where a redirect sends it."""

    meta = RuleMeta(
        id="DJI-010",
        title="Redirect target chosen by request data with no host check",
        family=Family.DJI,
        severity=Severity.MEDIUM,
        confidence=Confidence.FIRM,
        tier=Tier.STATIC,
        rationale=(
            "A redirect the client controls turns the application's own domain "
            "into the first hop of a phishing link. The victim sees a hostname "
            "they recognise, the certificate is genuine, and the site they land "
            "on is not. It is also how a login flow leaks its credentials: a "
            "next parameter pointing at an attacker's copy of the sign-in page "
            "is followed the moment authentication succeeds, and any token "
            "carried in the URL or a permissive referrer goes with it. Django "
            "rejects a redirect to an unusual scheme but sends one to any host, "
            "so the framework does not close this."
        ),
        remediation=(
            "Check the target before redirecting to it. django.utils.http "
            "provides url_has_allowed_host_and_scheme, which rejects an "
            "absolute URL, a protocol-relative one and the backslash and "
            "control-character variants that slip past a hand-written check; "
            "pass allowed_hosts=None to permit only relative paths, or the "
            "hosts you are willing to send a user to. Where the destination is "
            "one of a known few, take a key from the request and look the URL "
            "up rather than accepting it."
        ),
        references=(
            "https://cwe.mitre.org/data/definitions/601.html",
            "https://docs.djangoproject.com/en/stable/ref/request-response/#django.http.HttpResponseRedirect",
            "https://docs.djangoproject.com/en/stable/topics/http/shortcuts/#redirect",
            "https://cheatsheetseries.owasp.org/cheatsheets/Unvalidated_Redirects_and_Forwards_Cheat_Sheet.html",
        ),
        limitations=(
            "A target is reported only when taint analysis proves it came from "
            "the request. One read from a model field or a session is not "
            "reported, though a stored redirect is a real attack.",
            "A validator is recognised by what it does, not by what it returns. "
            "A function that parses a URL and reads its host is taken to be "
            "checking it, so one that reads the host for another purpose "
            "entirely would be honoured as a guard.",
            "What the guarding branch does with its answer is not examined, "
            "because deciding whether every path out of a check rejects the "
            "request or substitutes a default is how a rule begins reporting "
            "correct defensive code as a defect.",
            "Only the first argument is read. A redirect given a route name "
            "reverses a URL and cannot be made to name another host, which the "
            "constant in that position is what establishes.",
        ),
    )

    def admits(self, tree: ast.AST) -> bool:
        """Whether this file holds a redirect worth building a scope tree for."""
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for _ in redirects(node):
                    return True
        return False

    def check(self, ctx: ProjectContext) -> Iterator[Finding]:
        validators = self.validators(ctx)
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
            yield from self.inspect(ctx, path, root, validators)

    def validators(self, ctx: ProjectContext) -> frozenset[str]:
        """Every name in this project that decides where a URL points.

        Built once per run and by one pass over the source, rather than by
        following an import graph. A project-local validator is what two of the
        three benchmarks use, and a rule that could not see one would report
        their guarded redirects as findings.
        """
        found = set(VALIDATORS)
        for path in ctx.python_files:
            source = ctx.source(path)
            if source is None or not any(
                word in source for word in (*VALIDATORS, *PARSERS, "netloc", "hostname")
            ):
                continue
            tree = ctx.parse(path)
            if tree is None:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and validates(node):
                    found.add(node.name)
        return frozenset(found)

    def inspect(
        self, ctx: ProjectContext, path: Path, scope: Scope, validators: frozenset[str]
    ) -> Iterator[Finding]:
        here = [
            found
            for node in own_nodes(scope)
            if isinstance(node, ast.Call)
            for found in redirects(node)
        ]
        if here:
            chains = def_use(scope)
            for found in here:
                target = chosen(found.target, chains)
                if target is None:
                    continue
                if guarded(found.target, scope, chains, validators):
                    continue
                yield self.report(ctx, path, found, target)
        for child in scope.children:
            yield from self.inspect(ctx, path, child, validators)

    def report(self, ctx: ProjectContext, path: Path, found: Redirect, target: ast.expr) -> Finding:
        """Report one redirect. Always ``certain``, for `DJI-009`'s reason.

        :func:`chosen` resolves a name to its definition before deciding, and
        declines anything it cannot resolve, so every shape that reaches here
        has a request source to name. A lower-confidence branch would be one
        nothing could reach.
        """
        written = ast.unparse(target)
        source = _source_of(target)
        arrival = f"{source} reaches it" if source else f"{written} carries request data"
        return self.finding(
            location=ctx.location(path, found.call),
            confidence=Confidence.CERTAIN,
            message=(
                f"{found.name}() sends the client to {written}, and {arrival} "
                f"with nothing checking which host it names."
            ),
            evidence=(
                Evidence(
                    kind=EvidenceKind.AST,
                    content=ast.unparse(found.call),
                    source=f"{path}:{found.call.lineno}",
                ),
            ),
            properties={
                "sink": found.name,
                "target": written,
                "source": source or "unknown",
            },
        )


def _source_of(node: ast.expr) -> str | None:
    for inner in ast.walk(node):
        if isinstance(inner, ast.expr):
            found = request_source(inner)
            if found is not None:
                return found
    return None

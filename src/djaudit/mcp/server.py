"""A stdio JSON-RPC server speaking the Model Context Protocol.

Hand-written against the published schema rather than built on the reference
SDK. The SDK would bring pydantic, anyio, httpx and starlette into a package
whose entire runtime dependency list is typer and rich, and this process is
launched *inside a developer's editor by an agent*. The claim that makes
djaudit safe to point at unfamiliar code is that it parses and never executes;
a transport is not worth widening that surface for. What the protocol needs
from a tools-only server is four methods and a message loop.

Three things here are load-bearing and easy to get wrong.

**stdout is the wire.** A single stray ``print`` corrupts the stream and the
client sees a parse error rather than a tool result, so every handler runs with
stdout redirected to stderr. djaudit's own library layer does not print, but
the redirect makes that a property of this module rather than a standing
assumption about every rule anyone adds later.

**A tool failure is not a protocol failure.** The specification is explicit
that an error *inside* a tool belongs in the result with ``isError`` set, not
in a JSON-RPC error, because a protocol error is invisible to the model and it
cannot self-correct from it. A bad path is something the model should be told
about in words it can act on; only an unknown method is a protocol error.

**``instructions`` reaches the system prompt.** The initialize result carries a
field clients may add to the model's context, which is the difference between a
tool that is available and a tool that is used. It is where the loop is
actually specified.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from contextlib import redirect_stdout
from pathlib import Path
from typing import IO, Any

from djaudit import __version__, engine
from djaudit.llm.explain import FingerprintError, explain, find
from djaudit.llm.explain import render as render_explanation
from djaudit.models import Confidence, Family, Severity
from djaudit.registry import all_rules
from djaudit.reporters import json_reporter

PROTOCOL_VERSION = "2025-06-18"
"""What we implement. Older and newer clients are still answered -- see
:func:`_negotiate` -- because a tools-only server uses the part of the protocol
that has not changed across these revisions."""

_SPOKEN = frozenset({"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"})

INSTRUCTIONS = """\
djaudit is a deterministic Django analyser. It reads settings, models, DRF \
views, queries and migrations, and reports defects that are invisible in \
review: N+1 queries, SQL injection through string interpolation, unauthenticated \
endpoints, serializers that publish every column, unsafe migrations, and \
settings that are wrong only in production.

Use it as a verifier, not a linter. After you write or change Django code, call \
`audit_django_project` on the project root and repair what it reports before \
telling the user you are done. It is fast -- under four seconds on a \
1200-file project -- so there is no reason to skip it.

Findings are evidence-backed and measured at 100% precision on three real \
projects, so treat a finding as true. If one looks wrong, call \
`explain_django_finding` with its fingerprint: every rule documents what it \
deliberately does not claim, and the answer is usually there. Do not suppress \
a finding to make the audit pass.

Call `list_django_rules` before writing substantial Django code. Knowing what \
will be checked is cheaper than discovering it afterwards.\
"""

_PATH_SCHEMA = {
    "type": "string",
    "description": "Absolute path to the Django project root (the directory holding manage.py).",
}

TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "audit_django_project",
        "description": (
            "Audit a Django project and return every defect found. Covers security and "
            "deployment settings (DJS), SQL and shell injection (DJI), DRF authorization "
            "and exposure (DJA), ORM performance including N+1 queries (DJP), model design "
            "(DJD), migration safety (DJM) and SQLite/Postgres portability (DJX). Static "
            "analysis only: the project is parsed, never imported or executed. Call this "
            "after writing or changing Django code, and fix what it reports."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": _PATH_SCHEMA,
                "min_severity": {
                    "type": "string",
                    "enum": [s.value for s in Severity],
                    "description": "Hide findings below this severity. Defaults to 'low'.",
                },
                "families": {
                    "type": "array",
                    "items": {"type": "string", "enum": [f.value for f in Family]},
                    "description": "Restrict to these rule families. Omit to run all of them.",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "explain_django_finding",
        "description": (
            "Explain one finding in full: what the rule read, why it matters, how to fix "
            "it, what else in the file is wrong for the same reason, and what the rule "
            "deliberately does not claim. Use this when a finding looks like a false "
            "positive, or when the one-line remediation is not enough to act on."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": _PATH_SCHEMA,
                "fingerprint": {
                    "type": "string",
                    "description": (
                        "The finding's fingerprint from audit_django_project, or a "
                        "unique prefix of it."
                    ),
                },
            },
            "required": ["path", "fingerprint"],
        },
    },
    {
        "name": "list_django_rules",
        "description": (
            "List the rules djaudit checks, with the severity each carries. Read this "
            "before writing substantial Django code so the code is right the first time "
            "rather than repaired afterwards."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "family": {
                    "type": "string",
                    "enum": [f.value for f in Family],
                    "description": "Restrict to one family. Omit for the whole catalogue.",
                },
            },
        },
    },
)


class ToolError(Exception):
    """Something the caller can fix, phrased for the caller rather than a log."""


def _project_root(arguments: dict[str, Any]) -> Path:
    raw = arguments.get("path")
    if not isinstance(raw, str) or not raw.strip():
        raise ToolError("'path' is required and must be the Django project's root directory.")
    root = Path(raw).expanduser()
    if not root.is_dir():
        raise ToolError(f"Not a directory: {root}. Pass the directory holding manage.py.")
    return root


def _selected_families(arguments: dict[str, Any]) -> set[Family] | None:
    raw = arguments.get("families")
    if not raw:
        return None
    if not isinstance(raw, list):
        raise ToolError('\'families\' must be a list, for example ["DJS", "DJP"].')
    known = {f.value: f for f in Family}
    chosen = set()
    for name in raw:
        if name not in known:
            raise ToolError(f"Unknown family {name!r}. Known families: {', '.join(sorted(known))}.")
        chosen.add(known[name])
    return chosen


def _severity(arguments: dict[str, Any]) -> Severity:
    raw = arguments.get("min_severity", Severity.LOW.value)
    try:
        return Severity(raw)
    except ValueError:
        allowed = ", ".join(s.value for s in Severity)
        raise ToolError(f"Unknown severity {raw!r}. Use one of: {allowed}.") from None


def _summarise(report: dict[str, Any]) -> str:
    """The findings as the model has to read them: what, where, and what to do."""
    findings = report["findings"]
    blocking = [d for d in report.get("diagnostics", ()) if d.get("blocking")]
    lines: list[str] = []
    if blocking:
        lines.append("ANALYSIS INCOMPLETE -- the result below does not cover the whole project:")
        lines.extend(f"  {d['code']}: {d['message']}" for d in blocking)
        lines.append("")
    if not findings:
        lines.append("No findings. This project is clean at the requested thresholds.")
        return "\n".join(lines)

    lines.append(f"{len(findings)} finding(s), most severe first:")
    order = {s.value: i for i, s in enumerate(Severity)}
    for finding in sorted(findings, key=lambda f: (order[f["severity"]], f["rule_id"])):
        where = finding["location"]
        lines.append("")
        lines.append(
            f"{finding['rule_id']}  {finding['severity']}  "
            f"{where['file']}:{where['line']}  [{finding['fingerprint']}]"
        )
        lines.append(f"  {finding['title']}")
        lines.append(f"  {finding['message']}")
        lines.append(f"  FIX: {finding['remediation']}")
    lines.append("")
    lines.append(
        "Repair these in the source, then call audit_django_project again to confirm. "
        "Do not suppress a finding to make the audit pass."
    )
    return "\n".join(lines)


def _audit(arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    root = _project_root(arguments)
    result = engine.run(
        root,
        families=_selected_families(arguments),
        min_severity=_severity(arguments),
        min_confidence=Confidence.FIRM,
    )
    report = json_reporter.build(result)
    return _summarise(report), report


def _explain(arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    root = _project_root(arguments)
    fingerprint = arguments.get("fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint.strip():
        raise ToolError("'fingerprint' is required. Take it from audit_django_project.")
    result = engine.run(root, min_confidence=Confidence.FIRM)
    try:
        finding = find(result.findings, fingerprint)
    except FingerprintError as exc:
        raise ToolError(str(exc)) from exc
    text = render_explanation(explain(finding, result.findings))
    return text, {"rule_id": finding.rule_id, "fingerprint": finding.fingerprint}


def _rules(arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    wanted = arguments.get("family")
    if wanted is not None and wanted not in {f.value for f in Family}:
        raise ToolError(f"Unknown family {wanted!r}.")
    catalogue = [
        {
            "id": rule.meta.id,
            "family": rule.meta.family.value,
            "severity": rule.meta.severity.value,
            "title": rule.meta.title,
            "remediation": rule.meta.remediation,
        }
        for rule in all_rules()
        if wanted is None or rule.meta.family.value == wanted
    ]
    lines = [f"{len(catalogue)} rule(s):", ""]
    lines += [f"{r['id']}  {r['severity']:8s} {r['title']}" for r in catalogue]
    return "\n".join(lines), {"rules": catalogue}


_HANDLERS = {
    "audit_django_project": _audit,
    "explain_django_finding": _explain,
    "list_django_rules": _rules,
}


def _call_tool(params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name", "")
    arguments = params.get("arguments") or {}
    handler = _HANDLERS.get(name)
    if handler is None:
        known = ", ".join(sorted(_HANDLERS))
        return _tool_failure(f"Unknown tool {name!r}. This server offers: {known}.")
    try:
        # Redirected because stdout is the protocol wire. Anything a rule or a
        # future dependency prints would otherwise arrive mid-message.
        with redirect_stdout(sys.stderr):
            text, structured = handler(arguments)
    except ToolError as exc:
        return _tool_failure(str(exc))
    except Exception as exc:
        return _tool_failure(f"djaudit failed on this project: {type(exc).__name__}: {exc}")
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": structured,
        "isError": False,
    }


def _tool_failure(message: str) -> dict[str, Any]:
    """An error the model can read and act on, rather than a protocol fault."""
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _negotiate(requested: object) -> str:
    """Answer in the client's dialect when we know it, ours when we do not."""
    if isinstance(requested, str) and requested in _SPOKEN:
        return requested
    return PROTOCOL_VERSION


def _initialize(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "protocolVersion": _negotiate(params.get("protocolVersion")),
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": "djaudit", "version": __version__},
        "instructions": INSTRUCTIONS,
    }


def _dispatch(method: str, params: dict[str, Any]) -> dict[str, Any]:
    if method == "initialize":
        return _initialize(params)
    if method == "tools/list":
        return {"tools": list(TOOLS)}
    if method == "tools/call":
        return _call_tool(params)
    if method == "ping":
        return {}
    raise LookupError(method)


def _respond(message: dict[str, Any]) -> dict[str, Any] | None:
    """One request in, one response out -- or None for a notification."""
    identifier = message.get("id")
    method = message.get("method", "")
    if identifier is None:
        return None
    params = message.get("params") or {}
    try:
        result = _dispatch(method, params if isinstance(params, dict) else {})
    except LookupError:
        return {
            "jsonrpc": "2.0",
            "id": identifier,
            "error": {"code": -32601, "message": f"method not found: {method}"},
        }
    return {"jsonrpc": "2.0", "id": identifier, "result": result}


def _messages(stream: IO[str]) -> Iterator[dict[str, Any] | None]:
    """Parsed lines. A malformed line yields None rather than ending the session."""
    for line in stream:
        text = line.strip()
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            yield None
            continue
        yield parsed if isinstance(parsed, dict) else None


def serve(stdin: IO[str] | None = None, stdout: IO[str] | None = None) -> int:
    """Read requests until the client closes stdin.

    Returns an exit code so the CLI can hand it straight to the shell, though
    in practice the client terminates this process by closing the pipe.
    """
    source = stdin if stdin is not None else sys.stdin
    sink = stdout if stdout is not None else sys.stdout
    for message in _messages(source):
        if message is None:
            _write(sink, {"jsonrpc": "2.0", "id": None, "error": _parse_error()})
            continue
        response = _respond(message)
        if response is not None:
            _write(sink, response)
    return 0


def _parse_error() -> dict[str, Any]:
    return {"code": -32700, "message": "parse error: each line must be one JSON-RPC object"}


def _write(sink: IO[str], payload: dict[str, Any]) -> None:
    # No embedded newlines: the transport is newline-delimited, so a pretty
    # printed message would be read as several malformed ones.
    sink.write(json.dumps(payload) + "\n")
    sink.flush()

"""Check `action.yml` against the CLI it wraps.

A composite action is a string that is only executed on someone else's runner.
A flag that no longer exists, an input referenced by a name that was never
declared, a `run` step missing its `shell:` -- none of these are visible until
a workflow somewhere fails, and the failure is reported against the caller's
repository rather than ours.

So everything checkable without a runner is checked here:

* every `${{ inputs.x }}` names a declared input, because an undeclared one
  expands to the empty string rather than erroring;
* every declared input is used, because an input the steps ignore is a
  documented lie;
* every `--flag` handed to `djaudit run` exists on `djaudit run`;
* every default that names a CLI choice is one of that option's choices;
* the upload sits between the audit and the failure, because a composite
  action stops at its first failing step and an audit that failed early would
  take the upload with it.
"""

from __future__ import annotations

import pathlib
import re
import sys
from typing import Any

import typer.main
import yaml

from djaudit.cli import app

ROOT = pathlib.Path(__file__).resolve().parent.parent
ACTION = ROOT / "action.yml"
DOC = ROOT / "docs/github-action.md"

# Inputs whose value is passed straight to an option that only accepts certain
# words. A typo here is a runtime failure on the caller's runner.
CHOICE_INPUTS = {
    "fail-on": "--fail-on",
    "min-severity": "--min-severity",
    "min-confidence": "--min-confidence",
}

# `if:` conditions are expressions already, so they reference inputs without
# the `${{ }}` wrapper. Matching only the wrapped form reports every such
# input as unused.
REFERENCE = re.compile(r"(?<![\w.])inputs\.([a-z0-9_-]+)")
FLAG = re.compile(r"(?<![\w-])--[a-z][a-z-]*")


def _run_command() -> Any:
    """Return the click command behind `djaudit run`.

    Typer vendors click rather than depending on it, so this reads the objects
    structurally instead of importing click and asserting types against it.
    """
    command = typer.main.get_command(app)
    return command.commands["run"]  # type: ignore[attr-defined]


def _flags(command: Any) -> set[str]:
    return {o for p in command.params for o in getattr(p, "opts", []) if o.startswith("--")}


def _choices(command: Any, flag: str) -> list[str]:
    for param in command.params:
        if flag in getattr(param, "opts", []):
            return list(getattr(param.type, "choices", []))
    return []


def _text(node: Any) -> str:
    """Flatten a YAML subtree to every string it contains."""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        return " ".join(f"{k} {_text(v)}" for k, v in node.items())
    if isinstance(node, list):
        return " ".join(_text(v) for v in node)
    return str(node)


def _table(text: str, header: str) -> set[str]:
    """Read the first backticked cell of every row under a table header.

    Comparing the tables exactly, in both directions, is the point: a
    heuristic that guesses which backticked words are input names would miss a
    documented input that no longer exists, which is the worse of the two
    failures because it reads as supported and silently does nothing.
    """
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith(f"| {header} "):
            names = set()
            for row in lines[i + 2 :]:
                if not row.startswith("|"):
                    break
                if cell := re.match(r"\|\s*`([^`]+)`", row):
                    names.add(cell.group(1))
            return names
    return set()


def _check_doc(inputs: dict[str, Any], outputs: dict[str, Any]) -> list[str]:
    """Require the documented interface to be exactly the real one."""
    if not DOC.exists():
        return [f"{DOC.relative_to(ROOT)} is missing"]
    text = DOC.read_text()
    where = DOC.relative_to(ROOT)
    found: list[str] = []
    for kind, declared in (("input", set(inputs)), ("output", set(outputs))):
        documented = _table(text, kind.capitalize())
        for name in sorted(declared - documented):
            found.append(f"{kind} {name!r} is not documented in {where}")
        for name in sorted(documented - declared):
            found.append(f"{where} documents {kind} {name!r}, which does not exist")
    return found


def check() -> int:
    problems: list[str] = []
    if not ACTION.exists():
        print("::error::action.yml is missing")
        return 1

    action = yaml.safe_load(ACTION.read_text())
    runs = action.get("runs", {})
    if runs.get("using") != "composite":
        problems.append(f"runs.using is {runs.get('using')!r}, not 'composite'")

    steps = runs.get("steps", [])
    declared = set(action.get("inputs", {}))

    for step in steps:
        if "run" in step and step.get("shell") != "bash":
            problems.append(
                f"step {step.get('name')!r} runs a script without `shell: bash`, "
                "which a composite action rejects at load time"
            )

    referenced = set(REFERENCE.findall(_text(runs))) | set(
        REFERENCE.findall(_text(action.get("outputs", {})))
    )
    for name in sorted(referenced - declared):
        problems.append(
            f"`inputs.{name}` is used but never declared, so it expands to the empty string"
        )
    for name in sorted(declared - referenced):
        problems.append(f"input {name!r} is declared but never used, so it does nothing")

    audit = next((s for s in steps if s.get("id") == "audit"), None)
    if audit is None:
        problems.append("no step has `id: audit`, so its exit code cannot be carried forward")
    else:
        script = audit.get("run", "")
        known = _flags(_run_command())
        for flag in sorted(set(FLAG.findall(script))):
            if flag not in known:
                problems.append(f"the audit passes {flag}, which `djaudit run` does not accept")
        if "|| code=$?" not in script:
            problems.append(
                "the audit does not capture its exit code, so a finding would fail the step "
                "and the upload would never run"
            )

    names = [s.get("name") for s in steps]
    order = {name: i for i, name in enumerate(names)}
    upload = next((n for n in names if n and "upload" in n), None)
    verdict = next((n for n in names if n and "verdict" in n), None)
    if upload is None:
        problems.append("no step uploads the SARIF report")
    elif verdict is None:
        problems.append("no step reports the verdict, so a finding would never fail the job")
    elif not order["audit"] < order[upload] < order[verdict]:
        problems.append(
            "the steps must run audit, then upload, then the verdict; failing before the "
            "upload hides the findings it was meant to publish"
        )

    inputs = action.get("inputs", {})
    for name, flag in CHOICE_INPUTS.items():
        if name not in inputs:
            problems.append(f"input {name!r} is missing")
            continue
        default = str(inputs[name].get("default", ""))
        allowed = _choices(_run_command(), flag)
        if allowed and default not in allowed:
            problems.append(
                f"input {name!r} defaults to {default!r}, which {flag} does not accept "
                f"(accepts {', '.join(allowed)})"
            )

    problems.extend(_check_doc(inputs, action.get("outputs", {})))

    for problem in problems:
        print(f"::error::{problem}")
    if problems:
        return 1
    print(f"action.yml consistent: {len(declared)} inputs · {len(steps)} steps")
    return 0


def main() -> int:
    return check()


if __name__ == "__main__":
    sys.exit(main())

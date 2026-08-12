"""The configuration reference has to describe the configuration that exists.

A reference page is read for shape, not for truth. Nobody notices that a table
is missing a row, and nobody notices that a row describes a key the code
stopped accepting two releases ago -- which is exactly how this project nearly
shipped a reference for three settings that had never been built.

So the page is checked against the code rather than reviewed. Every key
`config.KNOWN` accepts must appear in the reference, every subtable must be
mentioned, every key the reference claims must be one the code accepts, and the
two settings a configuration file is forbidden to set must still be documented
as forbidden -- that last one because the refusal is a security property, and a
security property nobody wrote down is one the next person removes.

It also checks the flag column, since `--exclude-path` being documented as
`--exclude` is the same defect wearing a different hat.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import typer.main

from djaudit.cli import app
from djaudit.config import EXECUTING, KNOWN, SUBTABLES

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "configuration.md"
# A key named in the settings table, as `| `min_severity` | ...`
ROW = re.compile(r"^\|\s*`([a-z_]+)`\s*\|", re.MULTILINE)
FLAG = re.compile(r"`(--[a-z-]+)`")


def _run_options() -> set[str]:
    group = typer.main.get_command(app)
    run = group.commands["run"]
    return {opt for param in run.params for opt in param.opts if opt.startswith("--")}


def check() -> int:
    if not DOC.is_file():
        print(f"::error::{DOC.relative_to(ROOT)} does not exist")
        return 1
    text = DOC.read_text(encoding="utf-8")
    problems: list[str] = []

    documented = set(ROW.findall(text))

    # The executing keys are parsed but refused, so they belong in the refusal
    # section rather than in the table of settings you may actually use.
    usable = set(KNOWN) - set(EXECUTING)
    for key in sorted(usable):
        if key not in documented:
            problems.append(f"[tool.djaudit] accepts {key!r}, and the settings table omits it")

    for key in sorted(documented):
        if key not in KNOWN:
            problems.append(f"the settings table documents {key!r}, which the code does not accept")
        if key in EXECUTING:
            problems.append(f"the settings table offers {key!r}, which a config file may not set")

    for subtable in SUBTABLES:
        if f"[tool.djaudit.{subtable}]" not in text:
            problems.append(f"[tool.djaudit.{subtable}] is accepted and undocumented")

    for key, flag in sorted(EXECUTING.items()):
        # The literal statement, not the bare word: `--live` contains "live".
        if f"{key} = true" not in text:
            problems.append(f"a config file may not set {key!r}, and the page does not show it")
        if flag not in text:
            problems.append(f"{key!r} is refused in favour of {flag}, which the page never names")

    options = _run_options()
    for flag in sorted(set(FLAG.findall(text))):
        if flag not in options:
            problems.append(f"the page names {flag}, which `djaudit run` does not have")

    if problems:
        print("::error::the configuration reference does not match the configuration:")
        for problem in problems:
            print(f"  {problem}")
        return 1

    print(
        f"configuration reference current: {len(documented)} keys · "
        f"{len(SUBTABLES)} subtables · {len(EXECUTING)} refusals documented"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(check())

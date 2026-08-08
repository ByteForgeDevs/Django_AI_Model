"""Check that `docs/architecture/llm-layer.md` still describes the code.

A failure-modes document is the one kind of documentation where being out of
date is worse than being absent. Absent, a reader goes and looks; stale, they
believe it. So every checkable claim in that note is checked here, and this
script runs in the same gate as the tests.

"Checkable" means: a name that must exist, a number that must match, a file
that must be present. The arguments are not checkable and are not checked --
which is exactly why the note keeps them short and puts the evidence in code.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "architecture" / "llm-layer.md"
SRC = ROOT / "src" / "djaudit"


def fail(message: str) -> None:
    print(f"llm-layer.md: {message}")


def constant(module: Path, name: str) -> object:
    """Read a module-level constant without importing anything."""
    tree = ast.parse(module.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
        else:
            continue
        if name in targets and node.value is not None:
            try:
                return ast.literal_eval(node.value)
            except ValueError:
                return None
    raise KeyError(f"{module.name} has no {name}")


def dict_keys(module: Path, name: str) -> list[str]:
    tree = ast.parse(module.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign | ast.Assign):
            target = (
                node.target
                if isinstance(node, ast.AnnAssign)
                else next((t for t in node.targets if isinstance(t, ast.Name)), None)
            )
            if isinstance(target, ast.Name) and target.id == name:
                value = node.value
                if isinstance(value, ast.Dict):
                    return [
                        k.value
                        for k in value.keys
                        if isinstance(k, ast.Constant) and isinstance(k.value, str)
                    ]
    raise KeyError(f"{module.name} has no dict {name}")


def _measurements(text: str) -> list[str]:
    """The figures the note quotes, checked against where they were measured."""
    problems: list[str] = []
    edit_doc = (SRC / "llm" / "edit.py").read_text()
    for number in ("99.790", "86,601", "86,783", "3,159", "182"):
        if number not in text:
            problems.append(f"no longer reports the measured figure {number}")
        if number.replace(",", "") not in edit_doc.replace(",", ""):
            problems.append(f"reports {number}, which edit.py's measurement does not support")

    suite_timeout = constant(SRC / "llm" / "verify.py", "SUITE_TIMEOUT")
    if not isinstance(suite_timeout, int) or suite_timeout <= 0:
        problems.append("SUITE_TIMEOUT is no longer a positive integer")

    # The note claims only three modules touch a provider. Check it.
    allowed = {"triage.py", "explain.py", "impact.py", "provider.py", "budget.py", "cache.py"}
    for module in sorted((SRC / "llm").glob("*.py")):
        if module.name in allowed or module.name == "__init__.py":
            continue
        if re.search(r"\bProvider\b|\bprovider\.ask\b|\.ask\(", module.read_text()):
            problems.append(f"claims {module.name} never touches a provider, but it references one")
    return problems


def main() -> int:
    if not DOC.exists():
        fail("the file does not exist, but djaudit/llm/__init__.py cites it")
        return 1

    text = DOC.read_text()
    problems: list[str] = []

    # Every path the note names must exist. A doc that cites a file which was
    # renamed is how "see the test" becomes "trust me".
    cited = set(re.findall(r"`((?:src|tests|scripts|docs)/[\w./-]+)`", text))
    cited |= set(re.findall(r"`(tests/llm/test_\w+\.py)`", text))
    for path in sorted(cited):
        if not (ROOT / path).exists():
            problems.append(f"cites {path}, which does not exist")

    # Every symbol it names in backticks-with-parens or ALL_CAPS must resolve.
    triage = SRC / "llm" / "triage.py"
    verify = SRC / "llm" / "verify.py"
    fix = SRC / "llm" / "fix.py"

    minimum = constant(triage, "MINIMUM_OBSERVATIONS")
    claimed = re.search(r"`MINIMUM_OBSERVATIONS = (\d+)`", text)
    if not claimed:
        problems.append("no longer states MINIMUM_OBSERVATIONS")
    elif int(claimed.group(1)) != minimum:
        problems.append(f"says MINIMUM_OBSERVATIONS = {claimed.group(1)}, code says {minimum}")

    prior = dict_keys(triage, "CORPUS_PRIOR")
    claimed_rules = re.search(r"settles findings from (\d+) rules over (\d+)", text)
    if not claimed_rules:
        problems.append("no longer states the size of the corpus prior")
    elif int(claimed_rules.group(1)) != len(prior):
        problems.append(
            f"says the prior covers {claimed_rules.group(1)} rules, code has {len(prior)}"
        )

    fixers = dict_keys(fix, "FIXERS")
    for rule_id in fixers:
        if f"`{rule_id}`" not in text:
            problems.append(f"does not mention {rule_id}, which djaudit fix writes")
    for named in re.findall(r"`(DJS-\d{3})`", text):
        if named not in fixers and named != "DJS-007":
            problems.append(f"names {named} as fixable; FIXERS does not list it")
    if "DJS-007" not in text:
        problems.append("no longer explains why HSTS is excluded")

    # The verification levels, which the note leans on heavily.
    levels = verify.read_text()
    for level in ("STATIC", "TESTED"):
        if f"`Level.{level}`" not in text:
            problems.append(f"no longer mentions Level.{level}")
        if f"{level} = " not in levels:
            problems.append(f"claims Level.{level}, which verify.py does not define")

    problems.extend(_measurements(text))

    for problem in problems:
        fail(problem)
    if problems:
        return 1

    print(
        f"llm-layer.md consistent: {len(cited)} cited paths exist · "
        f"prior {len(prior)} rules · {len(fixers)} fixable rules"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

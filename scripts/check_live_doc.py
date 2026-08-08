"""Check that `docs/architecture/live-tier.md` still describes the code.

The live tier executes the audited project's code on the auditing machine, and
this note is the security model for that. A stale security note is worse than
an absent one: absent, a reader goes and looks; stale, they believe it. So
every checkable claim in it is checked here, in the same gate as the tests.

"Checkable" means a name that must exist, a number that must match, or a file
that must be present. The reasoning is not checkable and is not checked --
which is why the note keeps the reasoning short and puts the evidence in code.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "architecture" / "live-tier.md"
SRC = ROOT / "src" / "djaudit"
LIVE = SRC / "live"


def fail(message: str) -> None:
    print(f"live-tier.md: {message}")


def constant(module: Path, name: str) -> object:
    """Read a module-level constant without importing anything.

    Handles the three shapes these modules actually use: a literal, a
    `frozenset({...})` call, and a shift like `1 << 20`. `ast.literal_eval`
    reads none of the last two, and returning `None` for them would have made
    this gate check nothing while reporting success.
    """
    tree = ast.parse(module.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
        else:
            continue
        if name not in targets or node.value is None:
            continue
        return _value(node.value)
    raise KeyError(f"{module.name} has no {name}")


def _value(node: ast.expr) -> object:
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"frozenset", "set", "tuple", "list"}
        and node.args
    ):
        inner = _value(node.args[0])
        if isinstance(inner, list | set | frozenset | tuple):
            return frozenset(inner) if node.func.id == "frozenset" else inner
        return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.LShift):
        left, right = _value(node.left), _value(node.right)
        if isinstance(left, int) and isinstance(right, int):
            return left << right
        return None
    if isinstance(node, ast.Tuple):
        return tuple(_value(element) for element in node.elts)
    try:
        return ast.literal_eval(node)
    except ValueError:
        return None


def _environment(text: str) -> list[str]:
    """The passthrough and refusal sets, which are the security boundary."""
    problems: list[str] = []

    passthrough = constant(LIVE / "runner.py", "PASSTHROUGH")
    refused = constant(LIVE / "runner.py", "REFUSED")
    if not isinstance(passthrough, frozenset | set) or not isinstance(refused, frozenset | set):
        return ["PASSTHROUGH or REFUSED is no longer a literal set"]

    claimed = re.search(r"\*\*(\d+) variables\*\* cross", text)
    if not claimed:
        problems.append("no longer states how many variables cross into the subprocess")
    elif int(claimed.group(1)) != len(passthrough):
        problems.append(
            f"says {claimed.group(1)} variables cross, PASSTHROUGH has {len(passthrough)}"
        )

    # Naming them is the point: a reader checks the list, not the count.
    for name in sorted(passthrough):
        if f"`{name}`" not in text:
            problems.append(f"does not name {name}, which PASSTHROUGH lets through")

    claimed_refused = re.search(r"\*\*(\w+) variables a caller may not add", text)
    words = {"Two": 2, "Three": 3, "Four": 4, "Five": 5, "Six": 6}
    if not claimed_refused:
        problems.append("no longer states how many variables are refused")
    elif words.get(claimed_refused.group(1)) != len(refused):
        problems.append(
            f"says {claimed_refused.group(1)} variables are refused, REFUSED has {len(refused)}"
        )
    for name in sorted(refused):
        if f"`{name}`" not in text:
            problems.append(f"does not name {name}, which REFUSED rejects")

    # The claim that gives the boundary its teeth. A `PG*` entry would silently
    # redirect sqlmigrate at a database the project has never seen.
    if any(name.startswith("PG") for name in passthrough):
        problems.append("claims no PG* variable crosses, but PASSTHROUGH now contains one")
    return problems


def _limits(text: str) -> list[str]:
    problems: list[str] = []

    timeout = constant(LIVE / "runner.py", "DEFAULT_TIMEOUT")
    claimed = re.search(r"`DEFAULT_TIMEOUT` is\s+\*\*([\d.]+) seconds\*\*", text)
    if not claimed:
        problems.append("no longer states DEFAULT_TIMEOUT")
    elif float(claimed.group(1)) != timeout:
        problems.append(f"says DEFAULT_TIMEOUT is {claimed.group(1)}, code says {timeout}")

    limit = constant(LIVE / "runner.py", "OUTPUT_LIMIT")
    claimed_limit = re.search(r"`OUTPUT_LIMIT` is \*\*(\d+) MiB\*\*", text)
    if not isinstance(limit, int):
        problems.append("OUTPUT_LIMIT is no longer an integer")
    elif not claimed_limit:
        problems.append("no longer states OUTPUT_LIMIT")
    elif int(claimed_limit.group(1)) << 20 != limit:
        problems.append(f"says OUTPUT_LIMIT is {claimed_limit.group(1)} MiB, code says {limit}")

    rules = constant(LIVE / "locks.py", "RULES")
    if rules is None:
        # `RULES` holds enum members, so `literal_eval` cannot read it. Count
        # the tuples instead, which is the number the note quotes.
        tree = ast.parse((LIVE / "locks.py").read_text())
        rules = [
            node.value
            for node in tree.body
            if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "RULES"
        ]
        found = rules[0] if rules else None
        count = len(found.elts) if isinstance(found, ast.Tuple) else 0
    else:
        count = len(rules)  # type: ignore[arg-type]
    claimed_rules = re.search(r"\*\*(\d+) classification rules\*\*", text)
    if not claimed_rules:
        problems.append("no longer states how many lock classification rules there are")
    elif int(claimed_rules.group(1)) != count:
        problems.append(f"says {claimed_rules.group(1)} classification rules, locks.py has {count}")
    return problems


def _live_rules(text: str) -> list[str]:
    """Which rules need the tier at all. The note's reason for existing."""
    problems: list[str] = []
    live: list[str] = []
    for module in sorted((SRC / "rules").glob("*.py")):
        source = module.read_text()
        if re.search(r"tier\s*=\s*Tier\.LIVE", source):
            found = re.search(r'id\s*=\s*"(DJ[A-Z]-\d{3})"', source)
            if found:
                live.append(found.group(1))
    if not live:
        problems.append("found no live rules at all, so this check verified nothing")

    claimed = re.search(r"\*\*(\d+) declare `Tier\.LIVE`\*\*", text)
    if not claimed:
        problems.append("no longer states how many rules require the live tier")
    elif int(claimed.group(1)) != len(live):
        problems.append(f"says {claimed.group(1)} live rules, the registry has {len(live)}")
    for rule_id in live:
        if f"`{rule_id}`" not in text:
            problems.append(f"does not mention {rule_id}, which requires the live tier")
    return problems


def main() -> int:
    if not DOC.exists():
        fail("the file does not exist")
        return 1

    text = DOC.read_text()
    problems: list[str] = []

    # A doc that cites a renamed file is how "see the test" becomes "trust me".
    cited = set(re.findall(r"`((?:src|tests|scripts|docs)/[\w./-]+)`", text))
    for path in sorted(cited):
        if not (ROOT / path).exists():
            problems.append(f"cites {path}, which does not exist")

    # Line wrapping is not meaning. Matching against the raw text made the
    # checks below depend on where the paragraph happened to break, which is a
    # gate that reports a problem when someone reflows a sentence.
    flat = " ".join(text.split())
    problems += _environment(flat)
    problems += _limits(flat)
    problems += _live_rules(flat)

    # The environment variable the live tests key off. Named wrong, every
    # instruction in the note is a dead end.
    if "DJAUDIT_TEST_POSTGRES" not in text:
        problems.append("no longer names the environment variable the live tests read")
    for module in sorted((ROOT / "tests" / "live").glob("*.py")):
        if "DJAUDIT_TEST_POSTGRES" in module.read_text():
            break
    else:
        problems.append("names DJAUDIT_TEST_POSTGRES, which no live test reads")

    for problem in problems:
        fail(problem)
    if problems:
        return 1

    print(
        f"live-tier.md consistent: {len(cited)} cited paths exist · "
        f"{len(constant(LIVE / 'runner.py', 'PASSTHROUGH') or ())} passthrough vars · "  # type: ignore[arg-type]
        f"{len(constant(LIVE / 'runner.py', 'REFUSED') or ())} refused"  # type: ignore[arg-type]
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

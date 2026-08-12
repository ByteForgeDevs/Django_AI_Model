"""Check `docs/authoring-rules.md` against the API it describes, by using it.

A guide to writing rules is the one document whose claims are all executable,
so reviewing it is the wrong tool. This runs it: every fenced Python block is
compiled, the example rule is registered through the real `@register`, and the
finished rule is run over a real project by the real engine. If the example
stopped working, or the field it documents no longer exists, this fails.

The example registers a rule id that does not ship, so the registry is
snapshotted and restored around the run. Leaking `DJS-900` into a real audit
would be a worse bug than anything this gate catches.
"""

from __future__ import annotations

import ast
import dataclasses
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from djaudit import engine, models, registry  # noqa: E402
from djaudit.context import ProjectContext  # noqa: E402
from djaudit.models import Confidence, Severity  # noqa: E402
from djaudit.registry import Rule, RuleMeta  # noqa: E402

DOC = ROOT / "docs" / "authoring-rules.md"
BLOCK = re.compile(r"^```python\n(.*?)^```", re.MULTILINE | re.DOTALL)
# A field named in the reference table, as `| `severity` | ...`
ROW = re.compile(r"^\|\s*`([a-z_]+)`\s*\|", re.MULTILINE)

# `ctx.parse(path)` in prose or code -- the attribute a rule author will type.
CTX_ATTR = re.compile(r"\bctx\.([a-z_]+)")
# `Severity.CRITICAL`, `EvidenceKind.AST` -- an enum member the guide promises.
ENUM_MEMBER = re.compile(r"\b(Severity|Confidence|Tier|Family|EvidenceKind)\.([A-Z_]+)")
# A backticked repository path, as `src/djaudit/rules/_base.py`.
REPO_PATH = re.compile(r"`((?:src|tests|scripts|docs)/[A-Za-z0-9_./-]+)`")

EXAMPLE_ID = "DJS-900"
# The project the example is run against: one assert, in a file that parses.
TARGET = "def view(request):\n    assert request.user.is_staff\n    return None\n"


def _blocks(text: str) -> list[str]:
    return [m.group(1) for m in BLOCK.finditer(text)]


def _documented_fields(text: str) -> set[str]:
    return set(ROW.findall(text))


def _exec_blocks(blocks: list[str], problems: list[str]) -> dict[str, object]:
    """Run every block in one shared namespace, as a reader would top to bottom."""
    namespace: dict[str, object] = {"ast": ast}
    for index, block in enumerate(blocks, start=1):
        try:
            compiled = compile(block, f"<authoring-rules block {index}>", "exec")
        except SyntaxError as exc:
            problems.append(f"python block {index} does not parse: {exc}")
            continue
        try:
            exec(compiled, namespace)
        except NameError as exc:
            # A fragment may legitimately reference something defined by prose.
            if index == 1:
                problems.append(f"the example rule does not run: {exc}")
        except Exception as exc:
            problems.append(f"python block {index} raised {type(exc).__name__}: {exc}")
    return namespace


def _example(namespace: dict[str, object], problems: list[str]) -> type[Rule] | None:
    found = [
        value
        for value in namespace.values()
        if isinstance(value, type) and issubclass(value, Rule) and value is not Rule
    ]
    if not found:
        problems.append("no Rule subclass is defined in the guide")
        return None
    return found[0]


def _run_the_example(rule: type[Rule], problems: list[str]) -> None:
    """The whole point: the documented rule, through the real engine."""
    if rule.meta.id not in registry._REGISTRY:
        problems.append(f"the example rule {rule.meta.id} did not register")
        return
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "manage.py").write_text("import django\n", encoding="utf-8")
        (root / "views.py").write_text(TARGET, encoding="utf-8")
        result = engine.run(
            root,
            include={rule.meta.id},
            min_severity=Severity.INFO,
            min_confidence=Confidence.TENTATIVE,
        )
    if result.rule_errors:
        problems.append(f"the example rule raised: {result.rule_errors}")
        return
    ours = [f for f in result.findings if f.rule_id == rule.meta.id]
    if len(ours) != 1:
        problems.append(f"the example rule reported {len(ours)} findings on one assert, expected 1")
        return
    finding = ours[0]
    if not finding.evidence:
        problems.append("the example rule documents evidence but attaches none")
    elif "assert" not in finding.evidence[0].content:
        problems.append(f"the example's evidence does not quote the assert: {finding.evidence[0]}")
    if finding.location.line != 2:
        problems.append(f"the example reported line {finding.location.line}, expected 2")


def check() -> int:
    if not DOC.is_file():
        print(f"::error::{DOC.relative_to(ROOT)} does not exist")
        return 1
    text = DOC.read_text(encoding="utf-8")
    problems: list[str] = []

    real = {f.name for f in dataclasses.fields(RuleMeta)}
    documented = _documented_fields(text)
    for missing in sorted(real - documented):
        problems.append(f"RuleMeta.{missing} is not in the field table")
    for invented in sorted(documented - real - _other_table_keys(text)):
        problems.append(f"the field table documents {invented!r}, which is not a RuleMeta field")

    for attr in sorted(set(CTX_ATTR.findall(text))):
        if not hasattr(ProjectContext, attr) and attr not in ProjectContext.__annotations__:
            problems.append(f"the guide promises ctx.{attr}, which ProjectContext does not have")

    for enum_name, member in sorted(set(ENUM_MEMBER.findall(text))):
        enum = getattr(models, enum_name, None)
        if enum is None:
            problems.append(f"the guide names {enum_name}, which djaudit.models does not define")
        elif member not in enum.__members__:
            problems.append(f"the guide promises {enum_name}.{member}, which does not exist")

    for path in sorted(set(REPO_PATH.findall(text))):
        if not (ROOT / path).exists():
            problems.append(f"the guide names {path}, which is not in the repository")

    blocks = _blocks(text)
    if not blocks:
        problems.append("the guide contains no Python at all")

    # Builtins load lazily, so without this the registry is empty when the
    # guide's example registers and a reused id collides later, inside the
    # engine, as a traceback rather than as a diagnosis.
    registry.all_rules()
    before = dict(registry._REGISTRY)
    try:
        namespace = _exec_blocks(blocks, problems)
        rule = _example(namespace, problems)
        if rule is not None:
            if rule.meta.id in before:
                problems.append(f"the example uses {rule.meta.id}, which is a real shipped rule")
            else:
                _run_the_example(rule, problems)
    finally:
        registry._REGISTRY.clear()
        registry._REGISTRY.update(before)

    if problems:
        print("::error::the rule authoring guide does not match the API:")
        for problem in problems:
            print(f"  {problem}")
        return 1
    print(
        f"authoring guide current: {len(real)} fields · {len(blocks)} runnable blocks · "
        f"example {EXAMPLE_ID} runs"
    )
    return 0


def _other_table_keys(text: str) -> set[str]:
    """Rows from the paths table, which is keyed by directory rather than field."""
    return {key for key in ROW.findall(text) if "/" in key}


if __name__ == "__main__":
    raise SystemExit(check())

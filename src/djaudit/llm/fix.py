"""Turn a finding into a diff, for the small set of findings that permit one.

An autofixer earns trust by what it declines. This one writes a value only when
the value is decided by the rule rather than by the project, which -- measured
across the 27 `DJS` findings the fixtures produce -- is true of four rules out
of twenty-seven. Everything else is refused *by name*, with the reason, because
a silent skip reads as "nothing to do here".

**The agreement gate.** A fixer may only write a value that its own rule already
names as the value to set. `test_fix.py` asserts, for every entry in the table
below, that `"{setting} = {value}"` appears in that rule's `remediation`. So the
table cannot drift from the advice, and a fix cannot invent a setting the rule
never mentioned. The alternative -- a fixer that decides for itself what is
correct -- is a second, unreviewed rule engine hiding inside the first.

**Why `SECURE_HSTS_SECONDS` is deliberately absent.** It looks like the easiest
fix here: one integer, and `DJS-007`'s remediation names `31536000` outright, so
it passes the agreement gate. It is still refused, because the same remediation
says *"ramp up rather than jumping straight there -- a few minutes, then a day,
then a year"*. HSTS is sticky for as long as the max-age it rode in on, so a
one-step jump to a year is unrecoverable for a year if anything on the domain
cannot do HTTPS. A fixer that passed the gate and shipped advice its own rule
argues against would be worse than no fixer. The gate is necessary and not
sufficient, and this is the case that proves it.

**Preconditions travel with the fix.** `DJS-008` turns HSTS on for every
subdomain, which its rule permits "once every subdomain is served over HTTPS" --
something no static analyser can check. That fix is generated, because the
output is a diff for a human to accept, and labelled with the condition rather
than presented as ready.

**Nothing here writes a secret.** `DJS-002`, `DJS-003` and `DJS-005` are the
findings a naive fixer would most want to handle, and the fix they need is a
fresh key in an environment variable. Generating one into a diff would put a
live credential into a terminal, a patch file and a pull request. They are
refused, and separately, any finding whose snippet carries the engine's
redaction marker is refused before the table is consulted.
"""

from __future__ import annotations

import ast
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from djaudit.engine import RunResult
from djaudit.llm.edit import DIFF_CONTEXT, Edit, EditError, apply, diff, replace_value
from djaudit.models import Finding

# The engine masks secret values before they reach a finding. A masked snippet
# means the real text is not in our hands, so no patch built from it is honest.
REDACTION_MARKER = "<redacted:"


@dataclass(frozen=True)
class Fixer:
    """A settings assignment whose correct value the rule already decided."""

    setting: str
    value: str
    # A thing the tool cannot check and the operator must, or None when the fix
    # is unconditional. Rendered next to the diff, never suppressed.
    confirm: str | None = None


# Keyed by rule. Adding an entry means asserting the rule names this exact
# value; `test_the_table_agrees_with_every_rule_it_claims_to_fix` enforces it.
FIXERS: dict[str, Fixer] = {
    "DJS-001": Fixer("DEBUG", "False"),
    "DJS-008": Fixer(
        "SECURE_HSTS_INCLUDE_SUBDOMAINS",
        "True",
        confirm="every subdomain is served over HTTPS -- the directive is as sticky as the "
        "max-age it rides on, so one that cannot becomes unreachable for that long",
    ),
    "DJS-011": Fixer("SESSION_COOKIE_HTTPONLY", "True"),
    "DJS-018": Fixer("SECURE_CONTENT_TYPE_NOSNIFF", "True"),
}


def secret_lines(findings: Sequence[Finding]) -> dict[str, set[int]]:
    """Lines a rule already told us hold a credential, by file.

    The engine masks a secret before it reaches a finding, so `location.snippet`
    carrying the redaction marker is the run's own statement that the real text
    on that line must not be republished.
    """
    held: dict[str, set[int]] = {}
    for finding in findings:
        if REDACTION_MARKER in finding.location.snippet:
            held.setdefault(finding.location.file, set()).add(finding.location.line)
    return held


def safe_context(before: str, after: str, file: str, secrets: set[int]) -> int:
    """The most context that shows no line a rule flagged as a secret.

    A patch is read by people, pasted into pull requests and kept in CI logs,
    and unified-diff context is *unchanged* source printed verbatim. A fix to
    `DEBUG` three lines under `SECRET_KEY` will therefore publish the key while
    changing something else entirely -- the leak arrives through the context,
    not through the change.

    Narrowing rather than masking, because a masked context line makes the patch
    unappliable, and an unappliable patch is not a fix. Zero is the floor and
    still applies, though `git apply` wants `--unidiff-zero` for it.
    """
    if not secrets:
        return DIFF_CONTEXT
    for context in range(DIFF_CONTEXT, 0, -1):
        if not (_context_lines(before, after, file, context) & secrets):
            return context
    return 0


def _context_lines(before: str, after: str, file: str, context: int) -> set[int]:
    """The original line numbers a diff at this width would print unchanged."""
    shown: set[int] = set()
    line = 0
    for raw in diff(before, after, file, context).splitlines():
        if raw.startswith("@@"):
            line = int(raw.split("-", 1)[1].split(",")[0].split(" ")[0])
        elif raw.startswith(" "):
            shown.add(line)
            line += 1
        elif raw.startswith("-") and not raw.startswith("---"):
            line += 1
    return shown


@dataclass(frozen=True)
class Fix:
    """A proposed change to one file, and the finding that asked for it."""

    finding: Finding
    path: Path
    before: str
    edits: tuple[Edit, ...]
    confirm: str | None
    context: int = DIFF_CONTEXT

    @property
    def after(self) -> str:
        """The patched text, derived rather than stored.

        It was a field until a test in 6.4.3 set it to something the edits did
        not produce and `verify` never noticed -- because `combined` composes
        from `edits`, which is what actually ships. Two representations of one
        thing will eventually disagree, and the one being checked would have
        been the one nobody applies.
        """
        return apply(self.before, self.edits)

    @property
    def patch(self) -> str:
        return diff(self.before, self.after, self.finding.location.file, self.context)

    @property
    def ready(self) -> bool:
        """Whether a human still has to check something first."""
        return self.confirm is None


@dataclass(frozen=True)
class Refusal:
    """A finding no fix was offered for, and why not."""

    finding: Finding
    reason: str


def assignment_at(source: str, line: int, setting: str) -> ast.Assign | None:
    """The `setting = ...` statement on `line`, or None.

    Located through the parse tree and matched on the target's name. A text
    search would match the same characters inside a comment, a docstring or a
    dictionary key, and each of those appears in real settings modules.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or node.lineno != line:
            continue
        if any(isinstance(t, ast.Name) and t.id == setting for t in node.targets):
            return node
    return None


def fix_for(
    finding: Finding, root: Path, secrets: Sequence[Finding] | None = None
) -> Fix | Refusal:
    """Propose a change for one finding, or say why not.

    `secrets` is the rest of the run. It is used only to keep another finding's
    masked value out of this patch's context, and defaults to nothing so a
    single-finding caller still works -- with no context narrowing, which is
    why `fixes` always passes the whole run.
    """
    if REDACTION_MARKER in finding.location.snippet:
        return Refusal(
            finding,
            "the value is masked in the finding, so any patch built from it would be a guess",
        )
    fixer = FIXERS.get(finding.rule_id)
    if fixer is None:
        return Refusal(
            finding,
            f"no fix is defined for {finding.rule_id}: its correct value depends on the "
            "project rather than on the rule",
        )

    path = root / finding.location.file
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return Refusal(finding, f"{finding.location.file} could not be read: {exc}")

    node = assignment_at(source, finding.location.line, fixer.setting)
    if node is None:
        return Refusal(
            finding,
            f"line {finding.location.line} of {finding.location.file} is not an assignment to "
            f"{fixer.setting}; the file has moved on since the run",
        )
    if ast.dump(node.value) == ast.dump(ast.parse(fixer.value, mode="eval").body):
        return Refusal(finding, f"{fixer.setting} is already {fixer.value}")

    try:
        edit = replace_value(source, node.value, fixer.value, finding.rule_id)
        after = apply(source, [edit])
    except EditError as exc:
        return Refusal(finding, str(exc))

    held = secret_lines(secrets or ()).get(finding.location.file, set())
    return Fix(
        finding=finding,
        path=path,
        before=source,
        edits=(edit,),
        confirm=fixer.confirm,
        context=safe_context(source, after, finding.location.file, held),
    )


def fixes(run: RunResult, root: Path) -> tuple[list[Fix], list[Refusal]]:
    """Propose changes for a whole run, in finding order."""
    proposed: list[Fix] = []
    refused: list[Refusal] = []
    for finding in run.findings:
        outcome = fix_for(finding, root, run.findings)
        if isinstance(outcome, Fix):
            proposed.append(outcome)
        else:
            refused.append(outcome)
    return proposed, refused


def combined(proposed: Sequence[Fix]) -> dict[Path, str]:
    """One patched text per file, with every fix for that file applied.

    Two findings can land in one settings module, and two diffs against the
    same original would not compose -- the second would silently undo the
    first's line numbers when applied. Overlaps raise out of `apply`.
    """
    grouped: dict[Path, list[Fix]] = {}
    for fix in proposed:
        grouped.setdefault(fix.path, []).append(fix)
    return {
        path: apply(group[0].before, [e for f in group for e in f.edits])
        for path, group in grouped.items()
    }


def patch(proposed: Sequence[Fix]) -> str:
    """Every proposed change as one patch, applicable with `git apply`.

    Context is the narrowest any fix in a file asked for, so combining two
    fixes cannot widen the window past what one of them deliberately shrank.
    """
    parts = []
    for path, after in combined(proposed).items():
        group = [f for f in proposed if f.path == path]
        parts.append(
            diff(
                group[0].before,
                after,
                group[0].finding.location.file,
                min(f.context for f in group),
            )
        )
    return "".join(parts)

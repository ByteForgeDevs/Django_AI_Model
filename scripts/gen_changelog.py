"""Build the changelog from the commit trail, and check it still matches.

Generated rather than hand-written, for one reason: a hand-written changelog
omits whatever its author was in a hurry to finish, and being in a hurry
correlates with the changes most worth announcing. The commit trail is already
written, already reviewed, and already the thing that actually shipped.

The part that is not boilerplate is the link to ``schema/contract.json``. A
release that introduces a new ``SCHEMA_VERSION`` or ``FINGERPRINT_VERSION`` is
breaking in a way the user must act on -- a new fingerprint version means every
baseline they have committed needs regenerating, and if they find that out from
a thousand reopened findings instead of from this file, they will assume the
tool is broken. So the gate refuses to accept a changelog whose entry for a
release does not mention the versions that release changed.

Usage::

    python scripts/gen_changelog.py            # write CHANGELOG.md
    python scripts/gen_changelog.py --check    # verify it, for CI
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from djaudit import __version__  # noqa: E402

CHANGELOG = ROOT / "CHANGELOG.md"
LEDGER = ROOT / "schema" / "contract.json"

# Conventional-commit types, in the order a reader cares about them. Types
# absent from this map are deliberately dropped: a changelog listing every
# formatting pass buries the entries that matter.
SECTIONS: tuple[tuple[str, str], ...] = (
    ("feat", "Added"),
    ("fix", "Fixed"),
    ("perf", "Performance"),
    ("refactor", "Changed"),
    ("docs", "Documentation"),
    ("build", "Packaging"),
)

SUBJECT = re.compile(r"^(?P<type>[a-z]+)(?:\((?P<scope>[^)]+)\))?(?P<bang>!)?: (?P<text>.+)$")

PREAMBLE = """# Changelog

Generated from the commit trail by `scripts/gen_changelog.py`; edit the commits,
not this file. `scripts/gen_changelog.py --check` keeps the two in step.

Versions follow [semantic versioning](https://semver.org), with the pre-1.0
caveat that the minor carries breakage below `1.0.0`. `docs/versioning.md`
explains what counts as a breaking change to the report schema and to
fingerprints, and therefore to any baseline you have committed.
"""


class Commit:
    __slots__ = ("body", "sha", "subject")

    def __init__(self, sha: str, subject: str, body: str) -> None:
        self.sha = sha
        self.subject = subject
        self.body = body

    @property
    def parsed(self) -> re.Match[str] | None:
        return SUBJECT.match(self.subject)

    @property
    def breaking(self) -> bool:
        """Conventional commits spell this two ways, and both are in use."""
        match = self.parsed
        return bool(match and match["bang"]) or "BREAKING CHANGE:" in self.body


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def tags() -> list[str]:
    """Release tags, oldest first."""
    out = _git("tag", "--list", "v*", "--sort=creatordate").split()
    return [t for t in out if re.fullmatch(r"v\d+\.\d+\.\d+", t)]


def commits(since: str | None, until: str) -> list[Commit]:
    """Commits in a range, excluding merges and changelog-only churn.

    Dropping commits that touch nothing but ``CHANGELOG.md`` is what stops this
    from chasing its own tail: writing the changelog would otherwise be a
    change the changelog had to describe.
    """
    span = f"{since}..{until}" if since else until
    raw = _git("log", span, "--no-merges", "--format=%H%x00%s%x00%b%x1e")
    found = []
    for chunk in raw.split("\x1e"):
        if not chunk.strip():
            continue
        sha, subject, body = chunk.strip("\n").split("\x00", 2)
        touched = _git("show", "--name-only", "--format=", sha).split()
        if touched and all(f == "CHANGELOG.md" for f in touched):
            continue
        found.append(Commit(sha, subject, body))
    return found


def _ledger_at(ref: str | None) -> dict[str, list[str]]:
    """Which contract versions existed at a ref."""
    rel = LEDGER.relative_to(ROOT).as_posix()
    if ref is None:
        text = LEDGER.read_text() if LEDGER.exists() else "{}"
    else:
        got = subprocess.run(
            ["git", "show", f"{ref}:{rel}"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        text = got.stdout if got.returncode == 0 else "{}"
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {"schemas": [], "fingerprints": []}
    return {
        "schemas": sorted(data.get("schemas", {})),
        "fingerprints": sorted(data.get("fingerprints", {})),
    }


def contract_changes(since: str | None) -> list[str]:
    """Contract versions introduced since a ref, as upgrade notes.

    Read from the ledger rather than from commit subjects, because the ledger
    is what actually shipped and a subject is what somebody remembered to type.
    """
    before, after = _ledger_at(since), _ledger_at(None)
    notes = []
    for added in sorted(set(after["schemas"]) - set(before["schemas"])):
        notes.append(
            f"**Report schema {added}.** The JSON report shape or its enum "
            f"vocabularies changed; see `docs/versioning.md`."
        )
    for added in sorted(set(after["fingerprints"]) - set(before["fingerprints"])):
        notes.append(
            f"**Fingerprints are now `{added}`.** Finding identity changed, so "
            f"existing baseline files no longer match and must be regenerated "
            f"with `djaudit run --write-baseline`, or every finding they held "
            f"will be reported as new."
        )
    return notes


def _entries(found: list[Commit]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for commit in found:
        match = commit.parsed
        if match is None:
            continue
        heading = dict(SECTIONS).get(match["type"])
        if heading is None:
            continue
        scope = f"**{match['scope']}:** " if match["scope"] else ""
        mark = "**BREAKING** " if commit.breaking else ""
        grouped.setdefault(heading, []).append(f"- {mark}{scope}{match['text']} ({commit.sha[:8]})")
    return grouped


def section(title: str, found: list[Commit], notes: list[str]) -> str:
    lines = [f"## {title}", ""]
    if notes:
        lines += ["### Upgrading", ""]
        lines += [f"- {n}" for n in notes]
        lines.append("")
    grouped = _entries(found)
    for _, heading in SECTIONS:
        if heading in grouped:
            lines += [f"### {heading}", "", *grouped[heading], ""]
    if not grouped and not notes:
        lines += ["_Nothing recorded._", ""]
    return "\n".join(lines)


def build() -> str:
    released = tags()
    parts = [PREAMBLE]
    latest = released[-1] if released else None
    unreleased = commits(latest, "HEAD")
    if unreleased or not released:
        parts.append(section(f"Unreleased ({__version__})", unreleased, contract_changes(latest)))
    for index in range(len(released) - 1, -1, -1):
        newer = released[index]
        older = released[index - 1] if index > 0 else None
        parts.append(section(newer.lstrip("v"), commits(older, newer), contract_changes(older)))
    return "\n".join(parts).rstrip() + "\n"


def _shallow() -> bool:
    """Report whether the checkout has a truncated history.

    A depth-1 clone rebuilds the changelog from a single commit and reports it
    stale, which reads as a content problem rather than a checkout problem.
    """
    return _git("rev-parse", "--is-shallow-repository").strip() == "true"


def check() -> int:
    problems: list[str] = []
    if _shallow():
        print(
            "::error::this is a shallow clone, so the commit trail is truncated "
            "and any changelog built from it would be wrong; check out with "
            "fetch-depth: 0"
        )
        return 1
    if not CHANGELOG.exists():
        print("::error::CHANGELOG.md is missing; run scripts/gen_changelog.py")
        return 1
    current = CHANGELOG.read_text()
    if current != build():
        problems.append("CHANGELOG.md is out of date; run scripts/gen_changelog.py")
    for note in contract_changes(tags()[-1] if tags() else None):
        marker = note.split(".")[0].strip("*")
        if marker not in current:
            problems.append(
                f"the contract gained {marker!r} but CHANGELOG.md does not say so; "
                f"a user whose baseline stops matching needs to read it here, not "
                f"work it out from the findings"
            )
    if problems:
        for problem in problems:
            print(f"::error::{problem}")
        return 1
    counted = len(commits(tags()[-1] if tags() else None, "HEAD"))
    print(f"changelog current: {len(tags())} released · {counted} commits unreleased")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify instead of writing")
    args = parser.parse_args(argv)
    if args.check:
        return check()
    CHANGELOG.write_text(build())
    print(f"wrote {CHANGELOG.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

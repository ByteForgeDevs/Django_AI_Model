"""Check that `docs/architecture/adapters.md` still describes the code.

This note is the decision record for what we take from other people's tools.
Its claims are the kind that rot quietly: a count of claims in a table, which
verdict a code carries, which adapter reaches the network. None of those break
a test when they change, so without this they would be checked by nobody.

Modelled on `check_live_doc.py`, and for the same reason: "checkable" means a
name that must exist, a number that must match, or a file that must be present.
The reasoning is not checkable and is not checked -- which is why the note keeps
the reasoning short and puts the evidence in code.

The corpus numbers (13,388 findings selected, 41 kept, 268 of ours, and the
zero-collision measurement) are deliberately *not* re-derived here. They need
three cloned repositories and a ruff run over 3,000 files, which belongs in the
corpus job and not in the fast one. What this checks is everything that can be
read from the source tree, which is where the rot actually happens.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from djaudit import adapters, engine
from djaudit.adapters.base import Claim
from djaudit.adapters.pip_audit import ARGUMENTS
from djaudit.adapters.pip_audit import CLAIMS as PIP_AUDIT
from djaudit.adapters.ruff import CLAIMS as RUFF

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "architecture" / "adapters.md"


def fail(message: str) -> None:
    print(f"adapters.md: {message}")


def _tools(text: str) -> list[str]:
    """The adapters, their order, and which of them phones home."""

    problems: list[str] = []
    every = adapters.every()
    names = [a.name for a in every]

    claimed = re.search(r"\*\*(\d+) adapters\*\* ship", text)
    if not claimed:
        problems.append("no longer states how many adapters ship")
    elif int(claimed.group(1)) != len(every):
        problems.append(f"says {claimed.group(1)} adapters ship, `every()` returns {len(every)}")

    for name in names:
        if f"`{name}`" not in text:
            problems.append(f"does not name {name}, which `every()` returns")

    # The order is a claim the note makes in prose, and it is the reason the
    # networked tool is last.
    positions = [text.index(f"`{name}`") for name in names if f"`{name}`" in text]
    if len(positions) == len(names) and positions != sorted(positions):
        problems.append("names the adapters in a different order than `every()` runs them")

    remote = adapters.REACHES_THE_NETWORK
    if not remote:
        problems.append("REACHES_THE_NETWORK is empty, so this check verified nothing")
    for name in sorted(remote):
        if not re.search(rf"\| `{re.escape(name)}` \|[^|]*\|\s*\*\*yes\*\*", text):
            problems.append(f"does not mark {name} as reaching the network, but the code does")
    for name in names:
        if name not in remote and re.search(
            rf"\| `{re.escape(name)}` \|[^|]*\|\s*\*\*yes\*\*", text
        ):
            problems.append(f"marks {name} as reaching the network, but the code does not")

    if "REACHES_THE_NETWORK" not in text:
        problems.append("no longer names the constant the disclosure is built from")
    return problems


def _claims(text: str) -> list[str]:
    """The claim tables, which are the whole point of the layer."""

    problems: list[str] = []

    for verdict in Claim:
        if f"`{verdict.name}`" not in text:
            problems.append(f"does not describe the {verdict.name} verdict, which the code has")

    counted = {
        verdict: sum(1 for claim in RUFF.claims if claim.claim is verdict) for verdict in Claim
    }
    claimed = re.search(
        r"ruff's table holds \*\*(\d+) claims\*\*: (\d+) adopted, (\d+) subsumed, (\d+) rejected",
        text,
    )
    if not claimed:
        problems.append("no longer states how ruff's claims break down")
    else:
        total, adopt, subsumed, rejected = (int(g) for g in claimed.groups())
        if total != len(RUFF.claims):
            problems.append(f"says ruff has {total} claims, the table has {len(RUFF.claims)}")
        for label, said, actual in (
            ("adopted", adopt, counted[Claim.ADOPT]),
            ("subsumed", subsumed, counted[Claim.SUBSUMED]),
            ("rejected", rejected, counted[Claim.REJECTED]),
        ):
            if said != actual:
                problems.append(f"says {said} {label} ruff claims, the table has {actual}")

    pip_claimed = re.search(r"`pip-audit`'s table holds \*\*(\d+) claim", text)
    if not pip_claimed:
        problems.append("no longer states how many claims pip-audit's table holds")
    elif int(pip_claimed.group(1)) != len(PIP_AUDIT.claims):
        problems.append(
            f"says pip-audit has {pip_claimed.group(1)} claims, "
            f"the table has {len(PIP_AUDIT.claims)}"
        )
    return problems


def _subsumption(text: str) -> list[str]:
    """The one verdict that deletes information, and its recorded shortfall."""

    problems: list[str] = []
    subsumed = [claim for claim in RUFF.claims if claim.claim is Claim.SUBSUMED]
    for claim in subsumed:
        windows = [
            text[max(0, m.start() - 120) : m.end() + 120]
            for m in re.finditer(rf"`{re.escape(claim.code)}`", text)
        ]
        if not windows:
            problems.append(f"does not name {claim.code}, which the table subsumes")
            continue
        # Both names appearing *somewhere* in the note is not the claim. The
        # pairing is stated twice -- once in prose, once as a table header --
        # and a doc-wide substring search stayed green when one of the two was
        # rewritten to name a different rule, leaving the note contradicting
        # itself. So every place the external code is mentioned near one of our
        # rule ids has to name the rule the table actually says covers it.
        named = {rule for window in windows for rule in re.findall(r"DJ[A-Z]-\d{3}", window)}
        wrong = sorted(named - set(claim.ours))
        if wrong:
            problems.append(
                f"pairs {claim.code} with {', '.join(wrong)}, "
                f"but the table says {', '.join(claim.ours)}"
            )
        for ours in claim.ours:
            if ours not in named:
                problems.append(
                    f"never states that {claim.code} is subsumed by {ours}, "
                    f"which is what the claim table says"
                )

    reported = matched = 0
    for target in ("healthchecks", "netbox", "pretix"):
        record = ROOT / "benchmarks" / "subsumption" / f"{target}.json"
        if not record.is_file():
            problems.append(f"the recorded shortfall for {target} is missing")
            continue
        data = json.loads(record.read_text())
        for entry in data["claims"].values():
            reported += entry["reported"]
            matched += entry["matched"]
        row = re.search(rf"\| {target} \| (\d+) \| (\d+) \|", text)
        if not row:
            problems.append(f"no longer gives a row for {target} in the subsumption table")
            continue
        said_reported, said_matched = int(row.group(1)), int(row.group(2))
        for label, said, actual in (
            ("reported", said_reported, sum(e["reported"] for e in data["claims"].values())),
            ("covered", said_matched, sum(e["matched"] for e in data["claims"].values())),
        ):
            if said != actual:
                problems.append(f"says {said} {label} on {target}, the record says {actual}")

    # The arithmetic in the paragraph below the table has to survive a re-record.
    totals = re.search(r"(\d+) locations are reported and (\d+)\s+are covered; of the (\d+)", text)
    if not totals:
        problems.append("no longer totals the subsumption table")
    else:
        said_reported, said_matched, said_left = (int(g) for g in totals.groups())
        if (said_reported, said_matched) != (reported, matched):
            problems.append(
                f"totals {said_reported} reported and {said_matched} covered, "
                f"the records total {reported} and {matched}"
            )
        elif said_left != reported - matched:
            problems.append(
                f"says {said_left} remain, {reported} - {matched} is {reported - matched}"
            )
    return problems


def _behaviour(text: str) -> list[str]:
    """Claims about the code's shape that a refactor would quietly falsify."""

    problems: list[str] = []

    if "external" not in engine.run.__annotations__:
        problems.append("says external findings are folded in by the engine, which has no seam")

    # `--disable-pip` is the flag the note singles out, and the note is only
    # worth having while it is the one actually passed.
    for flag in ("--disable-pip", "--no-deps", "--vulnerability-service"):
        if flag not in ARGUMENTS:
            problems.append(f"discusses {flag}, which pip-audit is no longer run with")
        if f"`{flag}" not in text:
            problems.append(f"does not name {flag}, which pip-audit is run with")
    if "osv" not in ARGUMENTS:
        problems.append("says the OSV service is used, which the arguments no longer select")

    if "--external" not in text:
        problems.append("no longer names the flag the whole layer is behind")
    return problems


def main() -> int:
    if not DOC.exists():
        fail("the file does not exist")
        return 1

    text = DOC.read_text()
    problems: list[str] = []

    # A doc that cites a renamed file is how "see the code" becomes "trust me".
    cited = set(re.findall(r"`((?:src|tests|scripts|docs|benchmarks)/[\w./-]+)`", text))
    for path in sorted(cited):
        if not (ROOT / path).exists():
            problems.append(f"cites {path}, which does not exist")

    # Line wrapping is not meaning: matching the raw text would make these
    # checks depend on where a paragraph happened to break.
    flat = " ".join(text.split())
    problems += _tools(flat)
    problems += _claims(flat)
    problems += _subsumption(flat)
    problems += _behaviour(flat)

    for problem in problems:
        fail(problem)
    if problems:
        return 1

    print(
        f"adapters.md consistent: {len(cited)} cited paths exist · "
        f"{len(adapters.every())} adapters · {len(RUFF.claims)} ruff claims · "
        f"{len(adapters.REACHES_THE_NETWORK)} reaching the network"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

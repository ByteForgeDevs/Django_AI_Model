"""The ruff adapter: twenty-five decisions, one per code ruff can produce here.

`ruff --select DJ,S` is the largest single source of findings available to this
project and the one most likely to damage it. Measured on the benchmark corpus
it reports 958 findings on healthchecks, 210 on netbox and 12,220 on pretix,
against djaudit's 41, 76 and 151. Excluding test code -- done in `base`, before
this table is consulted -- leaves 617 findings across 25 distinct codes, and
this module is a written decision about each one.

The decisions are not guesses. Every code below was read at its actual sites in
the three projects, and the counts in each `why` are what that reading found.
Two general results came out of it, and they explain most of the table.

**Ours are dataflow claims; ruff's are shape claims.** Our `DJI` rules fire when
*request data* reaches a dangerous call. ruff's `S` rules fire on the call. Over
all 617 findings there is exactly one code with any same-line overlap with a
djaudit finding, so almost nothing here is a duplicate -- but that is precisely
why so much of it is rejected. `S608` flags SQL built by an f-string whether or
not anything untrusted reaches it, and on mature code it is the f-string that is
common and the untrusted input that is rare.

**A rule that names things by their name finds names.** `S105`
"hardcoded-password-string" reports `CENSOR_TOKEN = '********'`,
`TOKEN_PREFIX = 'nbt_'`, a `SECRET_KEY_CHARSET` holding an alphabet, and
`ctx["email_password_status"] = "success"`. The identifier contains "token" or
"password", and that is the whole of the evidence.

Four codes are adopted, one is subsumed, and twenty are rejected with the
measurement that rejected them.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from djaudit.adapters.base import (
    Availability,
    Claim,
    Claimed,
    ClaimTable,
    External,
    Report,
)
from djaudit.adapters.process import probe_tool, run_tool
from djaudit.models import Confidence, Family, Severity

TOOL = "ruff"

SELECT = "DJ,S"
"""The two rule families worth asking ruff for.

`DJ` is Django-specific and `S` is flake8-bandit. Everything else ruff can do
is style, import order and typing hygiene, which is the target project's
business and not an audit finding.
"""

ARGUMENTS = ("check", "--select", SELECT, "--output-format", "json", "--no-cache", "--isolated")
"""`--isolated` is the load-bearing one.

Without it ruff reads the *target's* `pyproject.toml`, so a project that
narrows `select`, widens `ignore` or sets `per-file-ignores` changes what
djaudit reports about it. A project would then be able to silence our audit by
editing its own linter configuration, which is exactly backwards. `--no-cache`
is for determinism in the same spirit: a warm `.ruff_cache` in the target tree
belongs to whatever version of ruff last ran there.

The output goes to a file rather than a pipe -- see `collect`.
"""

CLAIMS = ClaimTable(
    tool=TOOL,
    claims=(
        # ---------------------------------------------------------------- adopt
        Claimed(
            code="S113",
            claim=Claim.ADOPT,
            why=(
                "13 findings, none of them arguable and none of them ours. A "
                "`requests.get` with no timeout waits forever by default, so one "
                "unresponsive peer holds a worker thread until the process is "
                "restarted. The sites are what makes it worth having: pretix "
                "calls its own update-check endpoint, Stripe's OAuth endpoint "
                "and two currency-rate services this way, and netbox does it "
                "inside a background job. No djaudit rule looks at outbound "
                "HTTP, and there is no judgement to make here -- either an "
                "argument bounds the wait or nothing does."
            ),
            title="Outbound request has no timeout",
            family=Family.DJP,
            severity=Severity.MEDIUM,
            confidence=Confidence.FIRM,
            remediation=(
                "Pass `timeout=` to the call. `requests` has no default, so "
                "leaving it off means an unbounded wait rather than a long one."
            ),
        ),
        Claimed(
            code="S324",
            claim=Claim.ADOPT,
            why=(
                "20 findings, all SHA-1, and no djaudit rule reads `hashlib` at "
                "all -- DJS-019 covers `PASSWORD_HASHERS` and nothing else. "
                "Adopted as tentative rather than firm because the tool cannot "
                "see what the digest is for, and both kinds are present: "
                "healthchecks hashes salted API keys for storage, which is the "
                "security use, and pretix builds a redis cache key and a PDF "
                "filename, which are not. A reviewer settles it in seconds; "
                "neither ruff nor djaudit can."
            ),
            title="Digest built with a weak hash function",
            family=Family.DJS,
            severity=Severity.MEDIUM,
            confidence=Confidence.TENTATIVE,
            remediation=(
                "For anything security-bearing use SHA-256 or better, and for "
                "passwords use Django's hashers rather than `hashlib`. Where "
                "the digest is only an identifier -- a cache key, a filename -- "
                "pass `usedforsecurity=False`, which both records the intent and "
                "silences the check."
            ),
        ),
        Claimed(
            code="DJ007",
            claim=Claim.ADOPT,
            why=(
                "This is DJA-008's claim about a class djaudit does not parse. "
                "`fields = '__all__'` on a `ModelForm` binds every current and "
                "future model field to user input, which is the same mass "
                "assignment DJA-008 reports for serializers and rejects for the "
                "same reason: the risk arrives later, when somebody adds "
                "`is_staff` to the model and the form silently starts accepting "
                "it. 5 findings, all in netbox's model forms."
            ),
            title="Form binds every model field",
            family=Family.DJA,
            severity=Severity.HIGH,
            confidence=Confidence.FIRM,
            remediation=(
                "List the fields the form is meant to accept. An allowlist stays "
                "correct when the model grows; `'__all__'` does not."
            ),
        ),
        Claimed(
            code="DJ006",
            claim=Claim.ADOPT,
            why=(
                "DJA-009's claim, again for forms rather than serializers. "
                "`exclude` is a denylist, so every field added to the model "
                "afterwards is accepted by default and the form has to be "
                "revisited to stay safe. 3 findings, and two of them exclude "
                "`user` -- the field that decides who owns the row, which is "
                "the case DJA-011 exists for."
            ),
            title="Form names what to reject instead of what to accept",
            family=Family.DJA,
            severity=Severity.MEDIUM,
            confidence=Confidence.FIRM,
            remediation=(
                "Replace `exclude` with `fields`. The list is longer once and "
                "then correct, instead of correct now and wrong after the next "
                "migration."
            ),
        ),
        # ------------------------------------------------------------- subsumed
        Claimed(
            code="DJ001",
            claim=Claim.SUBSUMED,
            why=(
                "DJD-002 owns this and says it better. 151 of the 179 findings "
                "outside test code carry `blank=True` -- on healthchecks and "
                "netbox, every one of them -- which is the case DJD-002 "
                "deliberately exempts, because a field declaring that empty is "
                "permitted input has answered the question. DJD-002 also reports "
                "once per model and names each column, so its 8 pretix findings "
                "cover all 28 columns DJ001 reports there: pretix's `Invoice` "
                "declares fifteen of them and they are one migration to fix, not "
                "fifteen separate alerts."
            ),
            ours=("DJD-002",),
        ),
        # ------------------------------------------------------------- rejected
        Claimed(
            code="S101",
            claim=Claim.REJECTED,
            why=(
                "101 findings outside test code, and 850 of healthchecks' 958 "
                "total inside it, where `assert` is what pytest is made of. The "
                "outside-test uses are internal invariants and type narrowing "
                "for mypy. The theoretical harm -- `python -O` strips asserts, "
                "including `assert request.user.is_authenticated` in one "
                "healthchecks decorator -- needs a deployment that runs Django "
                "under `-O`, which neither gunicorn nor uwsgi does and no "
                "project here asks for."
            ),
        ),
        Claimed(
            code="S308",
            claim=Claim.REJECTED,
            why=(
                "111 findings, the largest single source of noise left after "
                "test code is removed, and every one read was a developer "
                "string: `mark_safe(escape(s)...)`, which escapes first; badge "
                "and `<pre>` markup built from model fields in netbox's table "
                "columns; static `help_text`. DJI-011 makes the claim that "
                "matters -- request data marked as trusted HTML -- and makes it "
                "about where the string came from, which is the only thing that "
                "distinguishes the dangerous case from a template helper."
            ),
        ),
        Claimed(
            code="DJ008",
            claim=Claim.REJECTED,
            why=(
                "64 findings. A model with no `__str__` shows up as "
                "`Object (1)` in the admin. That is a usability preference, and "
                "on a mature codebase it is a house-style question with 64 "
                "answers, none of which is a defect a security or performance "
                "audit should be spending a reviewer's attention on."
            ),
        ),
        Claimed(
            code="DJ012",
            claim=Claim.REJECTED,
            why=(
                "44 findings about the order in which `Meta`, managers and "
                "methods appear in a model body. Pure style, and djaudit does "
                "not have opinions about where code sits in a file."
            ),
        ),
        Claimed(
            code="S110",
            claim=Claim.REJECTED,
            why=(
                "25 findings of `except Exception: pass`. A real smell and not a "
                "Django one, and on code this mature the sites read as "
                "deliberate -- optional imports, best-effort cleanup, a data "
                "migration that tolerates a missing row. Nothing here "
                "distinguishes a swallowed error from an ignored one."
            ),
        ),
        Claimed(
            code="S105",
            claim=Claim.REJECTED,
            why=(
                "20 findings, and the rule is matching identifiers rather than "
                "secrets: `CENSOR_TOKEN = '********'`, `TOKEN_PREFIX = 'nbt_'`, "
                "`MODULE_TOKEN = '{module}'`, a `SECRET_KEY_CHARSET` holding an "
                'alphabet, and `ctx["email_password_status"] = "success"`. '
                "The two that name a real key are a build-time placeholder and a "
                "test configuration module. DJS-002, DJS-004 and DJS-005 make "
                "this claim about resolved settings values, which is where a "
                "credential that matters actually lives."
            ),
        ),
        Claimed(
            code="S311",
            claim=Claim.REJECTED,
            why=(
                "6 findings, and the uses are scheduling jitter, two profiling "
                "sample rates and an 8-character display slug. `random` is a "
                "real vulnerability when it generates a token, and none of these "
                "generates one; ruff cannot tell the difference, and on this "
                "corpus the difference is all of them."
            ),
        ),
        Claimed(
            code="S608",
            claim=Claim.REJECTED,
            why=(
                "5 findings, all SQL assembled from internal values -- a table "
                "name from `_meta.db_table`, a subquery built by netbox's own "
                "search backend. DJI-001 asks the question that decides whether "
                "string-built SQL is a vulnerability, which is whether request "
                "data reaches it, and answers it for these five with no."
            ),
        ),
        Claimed(
            code="S607",
            claim=Claim.REJECTED,
            why=(
                "4 findings, all `npm` or a docs builder invoked from a build "
                "script that already runs with the developer's `PATH`. A partial "
                "executable path is a privilege-escalation finding when the "
                "caller is a service; here the caller is `make`."
            ),
        ),
        Claimed(
            code="S102",
            claim=Claim.REJECTED,
            why=(
                "3 findings, all of them netbox's scripting features doing what "
                "they are for: `nbshell` executing an operator's command and the "
                "custom-script loader executing a stored script. DJI-007 covers "
                "the case that is a defect, which is request data reaching an "
                "executing loader."
            ),
        ),
        Claimed(
            code="S603",
            claim=Claim.REJECTED,
            why=(
                "3 findings, and the rule fires on the *recommended* form. All "
                "three pass an argument list with no shell, which is precisely "
                "the fix S602 asks for. A check that flags the remedy for "
                "another check cannot be reported to a user as a defect."
            ),
        ),
        Claimed(
            code="S602",
            claim=Claim.REJECTED,
            why=(
                "2 findings, both `shell=True` with a string literal -- "
                "`'npm ci'` and `'npm run build'` -- in pretix's build script. "
                "`shell=True` matters when something composes the string; "
                "nothing composes these."
            ),
        ),
        Claimed(
            code="S112",
            claim=Claim.REJECTED,
            why="2 findings of `except Exception: continue`. Rejected with S110, for its reasons.",
        ),
        Claimed(
            code="S104",
            claim=Claim.REJECTED,
            why=(
                "1 finding, and it is a false positive: the string `0.0.0.0` "
                "appears in the *help text* of a `--host` argument describing "
                "the default. Nothing binds anything."
            ),
        ),
        Claimed(
            code="S605",
            claim=Claim.REJECTED,
            why=(
                "1 finding: healthchecks' Shell transport calling `os.system` on "
                "a command the operator configured. Running an operator-supplied "
                "command is the entire feature, and it is documented as such."
            ),
        ),
        Claimed(
            code="S701",
            claim=Claim.REJECTED,
            why=(
                "1 finding, a Jinja2 `Environment` with no autoescape in a "
                "script that renders netbox's API schema documentation. "
                "Autoescape off is dangerous when the output is HTML served to a "
                "browser; this output is a build artefact."
            ),
        ),
        Claimed(
            code="S108",
            claim=Claim.REJECTED,
            why=(
                "1 finding: `/tmp/netbox-smoketest` as the fallback of an "
                "environment variable in a smoke-test configuration module."
            ),
        ),
        Claimed(
            code="S611",
            claim=Claim.REJECTED,
            why=(
                "1 finding. `RawSQL` is not a defect by itself -- DJI-004 "
                "reports it when request data is interpolated into it, and this "
                "instance interpolates a queryset the caller already built."
            ),
        ),
        Claimed(
            code="S314",
            claim=Claim.REJECTED,
            why=(
                "1 finding, `ElementTree.fromstring` on a tax authority's SOAP "
                "response. CPython's `ElementTree` has not resolved external "
                "entities since 3.8, so the residual risk is an expansion bomb "
                "from a service the operator has already chosen to trust and "
                "authenticate to."
            ),
        ),
        Claimed(
            code="S310",
            claim=Claim.REJECTED,
            why=(
                "1 finding, `urlopen` against pretix's own vite dev server, with "
                "a timeout already passed. DJI-009 covers the case worth "
                "reporting, which is request data choosing the host."
            ),
        ),
    ),
)


def parse(payload: str, root: Path) -> tuple[External, ...]:
    """Turn ruff's JSON into `External` records.

    `filename` is absolute and `Location.file` is project-relative POSIX, so the
    conversion happens here rather than in any claim. A path outside the project
    is dropped rather than allowed through as an absolute string: it would be
    unresolvable to every reader of the report and unstable in a fingerprint.
    """
    items = json.loads(payload) if payload.strip() else []
    found: list[External] = []
    for item in items:
        try:
            relative = Path(item["filename"]).resolve().relative_to(root.resolve())
        except ValueError:
            continue
        end = item.get("end_location") or {}
        found.append(
            External(
                code=item["code"],
                message=item["message"],
                file=str(PurePosixPath(relative)),
                line=item["location"]["row"],
                column=item["location"]["column"],
                end_line=end.get("row"),
                end_column=end.get("column"),
                url=item.get("url") or "",
            )
        )
    return tuple(found)


@dataclass(frozen=True, slots=True)
class RuffAdapter:
    """Runs ruff and keeps the four codes worth keeping."""

    executable: str = TOOL

    @property
    def name(self) -> str:
        return TOOL

    @property
    def table(self) -> ClaimTable:
        return CLAIMS

    def probe(self) -> Availability:
        return probe_tool(self.executable, name=TOOL)

    def collect(self, root: object) -> Report:
        """Never raises for anything ruff does.

        The output goes to a temporary *file* rather than a pipe, because the
        pipe has a ceiling. `live.runner` caps each captured stream at 1 MiB so
        that a runaway subprocess cannot exhaust memory, and ruff's JSON for
        pretix is larger than that: 12,220 findings truncated mid-object at
        exactly 1,048,576 bytes, which `json` then rejects. Read from a pipe,
        the largest project in the corpus reported nothing at all and said only
        that its output could not be read. The cap is right and the fix belongs
        here. The file is ours, not the target's, so a read-only checkout still
        works and nothing is left in the project tree.

        ruff exits 1 when it has findings and 2 when it could not run, so the
        status alone cannot distinguish "found things" from "failed"; the
        payload decides. Anything unreadable becomes a diagnostic on a report
        that still lets the rest of the audit finish.
        """
        directory = Path(str(root))
        availability = self.probe()
        if not availability:
            return Report(tool=TOOL, availability=availability)

        with tempfile.TemporaryDirectory(prefix="djaudit-ruff-") as scratch:
            destination = Path(scratch) / "ruff.json"
            outcome = run_tool(
                [self.executable, *ARGUMENTS, "--output-file", str(destination), "."],
                root=directory,
            )
            try:
                payload = destination.read_text(encoding="utf-8")
            except OSError:
                payload = ""

        if not payload.strip():
            return Report(tool=TOOL, availability=availability, diagnostics=(outcome.describe(),))

        try:
            found = parse(payload, directory)
        except (ValueError, KeyError, TypeError) as exc:
            return Report(
                tool=TOOL,
                availability=availability,
                diagnostics=(f"{outcome.describe()}; its output could not be read: {exc}",),
            )

        return Report(
            tool=TOOL,
            availability=availability,
            findings=CLAIMS.adopt(found, availability),
            unclaimed=CLAIMS.unclaimed(found),
        )

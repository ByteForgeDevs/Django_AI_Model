"""What Django's own deployment check says, normalised into our schema.

`manage.py check --deploy` is the closest thing Django has to a first-party
security audit, and it knows things our static tier cannot: it sees settings
after every import, override and environment variable has been resolved, so a
`SECURE_SSL_REDIRECT` assembled at runtime is simply a value to it. Running it
is cheap and refusing to run it would be pride.

**Everything measured below is measured, because the output is designed for a
terminal and every convenient assumption about it is false.**

Django writes the report to **stderr** when there are issues and to **stdout**
when there are none, so a reader that watches one stream sees either the
findings or the all-clear but never both. The exit code is **0 for warnings**
and non-zero only at `--fail-level` or above, which means a deployment check
full of security warnings looks exactly like success. And when the project
cannot be imported at all the command dies with a traceback -- no sections, no
footer, nothing that parses -- which a careless reader scores as a clean bill of
health for a project it never managed to load.

So the footer is what we trust. `System check identified N issues (M silenced).`
is written by the same code path that writes the body, and its absence means we
did not get a report. Findings are only reported when it is present; anything
else is `Unknown`, carrying the reason.

**The format, from `django/core/checks/messages.py` and
`django/core/management/base.py`:**

```
CRITICALS:
ERRORS:
WARNINGS:
INFOS:
DEBUGS:
```

in that fixed order, each followed by lines of `"%s: %s%s%s" % (obj, id, msg,
hint)` where `obj` is `?` for a check about no particular object, the id is
`(security.W009) ` and is **omitted entirely** when the check has none, and the
hint is a `\t`-indented `HINT: ...` continuation line. We pass `--no-color`,
which was measured to beat both `--force-color` and `DJANGO_COLORS`.

**Silenced checks are invisible to us and that is the point of our static
tier.** `SILENCED_SYSTEM_CHECKS = ['security.W009']` removes the message from
the body entirely; only the footer's count changes. Django will not tell us
which id was silenced, so a project can quiet its deployment check without
quieting the risk -- and our own `DJS` rules, which read the settings source
rather than asking Django, still see it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import NamedTuple

from djaudit.live.runner import DEFAULT_TIMEOUT, Outcome, run_python
from djaudit.live.sqlmigrate import Target
from djaudit.models import Severity

SECTIONS: Mapping[str, Severity] = {
    "CRITICALS": Severity.CRITICAL,
    "ERRORS": Severity.HIGH,
    "WARNINGS": Severity.MEDIUM,
    "INFOS": Severity.LOW,
    "DEBUGS": Severity.INFO,
}
"""Django's five levels, mapped onto ours.

`CRITICAL` and `ERROR` are Django's own words for "this project is broken", so
they keep their weight. A deployment `WARNING` is `MEDIUM` rather than `HIGH`
because the deployment checks fire on defaults as much as on mistakes -- a
project that has not set `SECURE_HSTS_SECONDS` yet is not the same as one that
has turned it off -- and our own `DJS` rules are the ones that read the source
and can tell those apart.
"""

FOOTER = re.compile(
    r"^System check identified (?:no issues|1 issue|(?P<count>\d+) issues) "
    r"\((?P<silenced>\d+) silenced\)\.$",
    re.MULTILINE,
)
"""The proof that a report was produced rather than a process merely exiting."""

MESSAGE = re.compile(r"^(?P<obj>\S.*?): (?:\((?P<id>[\w.]+)\) )?(?P<msg>.*)$")
"""`obj` is non-greedy up to the first `": "`, because a check id is never in
the object but a colon can be: `blog.Post.a` and `?` are both objects, and the
message itself frequently contains `": "` further along."""

HINT = "\tHINT: "


class Message(NamedTuple):
    """One line of Django's report."""

    severity: Severity
    obj: str
    id: str | None
    """`None` for a check that declares no id. Rare, but `MESSAGE` allows it
    because `CheckMessage.__str__` does."""

    text: str
    hint: str | None

    @property
    def about_nothing(self) -> bool:
        """Whether the check names no object. Settings checks all do this, so
        `?` is the normal case for `--deploy` rather than an oddity."""
        return self.obj == "?"

    def describe(self) -> str:
        return f"{self.id or 'no id'}: {self.text}"


class Report(NamedTuple):
    """A deployment check that ran to completion."""

    messages: tuple[Message, ...]
    silenced: int
    """How many checks were suppressed by `SILENCED_SYSTEM_CHECKS`. Django
    reports the count and never the ids, so this is the whole of what we can
    say about them."""

    @property
    def available(self) -> bool:
        return True

    def by_id(self, check: str) -> Message | None:
        return next((m for m in self.messages if m.id == check), None)

    def ids(self) -> frozenset[str]:
        return frozenset(m.id for m in self.messages if m.id is not None)

    def explain(self) -> str:
        return f"{len(self.messages)} checks reported, {self.silenced} silenced"


class Unknown(NamedTuple):
    """The deployment check did not produce a report. Carried, not raised."""

    why: str

    @property
    def available(self) -> bool:
        return False

    @property
    def silenced(self) -> int:
        return 0

    @property
    def messages(self) -> tuple[Message, ...]:
        return ()

    def by_id(self, check: str) -> Message | None:
        return None

    def ids(self) -> frozenset[str]:
        return frozenset()

    def explain(self) -> str:
        return f"the deployment check did not report: {self.why}"


def parse_report(text: str) -> Report | Unknown:
    """Read Django's report, or say why there is none to read.

    Takes the two streams already joined: which one carried the report depends
    on whether there were issues, and by here that no longer matters.
    """
    footer = FOOTER.search(text)
    if footer is None:
        return Unknown("no summary line, so the command did not finish a check")

    messages: list[Message] = []
    severity: Severity | None = None
    for line in text.splitlines():
        if line.startswith(HINT):
            if messages:
                messages[-1] = messages[-1]._replace(hint=line[len(HINT) :])
            continue
        section = line.rstrip(":")
        if line.endswith(":") and section in SECTIONS:
            severity = SECTIONS[section]
            continue
        if severity is None:
            continue
        found = MESSAGE.match(line)
        if found is None:
            continue
        messages.append(
            Message(
                severity=severity,
                obj=found["obj"],
                id=found["id"],
                text=found["msg"],
                hint=None,
            )
        )
    return Report(tuple(messages), int(footer["silenced"]))


def _refusal(outcome: Outcome) -> str:
    tail = (outcome.stderr or outcome.stdout).strip().splitlines()
    if not tail:
        return f"exit code {outcome.returncode} and no output"
    return tail[-1][:200]


def run_deployment_check(
    target: Target,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    extra_environment: Mapping[str, str] | None = None,
) -> Report | Unknown:
    """Run `manage.py check --deploy` in the target's own interpreter.

    Both streams are read and joined. The exit code is deliberately not
    consulted: it is 0 for a project full of security warnings and non-zero for
    a project with one unrelated model error, so it answers a different
    question than the one being asked. Whether a report exists is decided by
    whether the report is there.
    """
    outcome = run_python(
        target.interpreter,
        [target.manage_py.name, "check", "--deploy", "--no-color"],
        cwd=target.manage_py.parent,
        timeout=timeout,
        extra_environment=extra_environment,
    )
    report = parse_report(outcome.stdout + "\n" + outcome.stderr)
    if not report.available and outcome.returncode != 0:
        return Unknown(_refusal(outcome))
    return report

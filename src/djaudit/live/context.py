"""What the target's own Django says about itself.

The static tier infers the Django version from a lockfile and the settings
module from an assignment in `manage.py`. Both are guesses, and both are wrong
in the cases that matter most: a version range that resolved to something else,
a settings module chosen by an environment variable at deploy time, a
`DATABASES` dict assembled from `dj_database_url`. This module asks instead of
inferring, by starting the target's own interpreter and reading the answer.

**It asks for named facts and never dumps settings.** This is the property the
rest of the module is arranged around. `manage.py diffsettings` would answer
almost every question in one call, and its output contains `SECRET_KEY`, every
database password, and every API token the project keeps in settings. Those
answers become a finding's evidence, and evidence is written into SARIF, which
is uploaded to GitHub code scanning and kept. A security tool that copies
production credentials into a security dashboard has introduced a worse problem
than any it could report. So `QUESTIONS` is an allowlist, exactly as
`PASSTHROUGH` is in the runner, and anything not named there is not collected.

Where a rule needs to judge a secret, it must ask for a *predicate* rather than
the value -- whether the key is shorter than Django's own floor, not the key.

**Every answer is optional and failure is recorded rather than raised.** A
target whose database is unreachable still has a resolvable `DATABASES` setting,
and a target with one broken app still has a Django version. Each question is
evaluated on its own and a question that fails leaves a note in `problems`
instead of costing us the other twelve answers. 4.1.4 turns those notes into
the degradation report.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, NamedTuple

from djaudit.live.interpreter import Interpreter
from djaudit.live.runner import Outcome, run_python

OPEN = "<<<DJAUDIT-BEGIN>>>"
CLOSE = "<<<DJAUDIT-END>>>"
"""Delimiters around the payload.

The target owns stdout and uses it: a `settings.py` that prints a startup line,
a `manage.py` that announces which configuration it picked, a library that
greets on import. All of that lands in front of our JSON, and it was measured
rather than feared -- one `print` in a settings module is enough. Framing the
answer means we can find it inside whatever else the target chose to say.
"""

DEFAULT_TIMEOUT = 60.0
"""Generous because a project's `manage.py` may read a `.env` file, resolve a
settings module that imports a dozen others, and on a cold filesystem do all of
it slowly. Nothing here loads the app registry, so this is a ceiling rather
than an expectation."""

QUESTIONS: tuple[tuple[str, str], ...] = (
    ("django_version", "django.get_version()"),
    ("python_version", "'.'.join(str(p) for p in sys.version_info[:3])"),
    ("settings_module", "settings.SETTINGS_MODULE"),
    ("debug", "bool(settings.DEBUG)"),
    ("databases", "{a: c.get('ENGINE') for a, c in settings.DATABASES.items()}"),
    ("installed_apps", "[str(a) for a in settings.INSTALLED_APPS]"),
    ("use_tz", "bool(settings.USE_TZ)"),
    ("allowed_hosts", "[str(h) for h in settings.ALLOWED_HOSTS]"),
    ("middleware", "[str(m) for m in settings.MIDDLEWARE]"),
    ("default_auto_field", "str(getattr(settings, 'DEFAULT_AUTO_FIELD', ''))"),
)
"""The facts we collect, and by omission the ones we refuse to.

`DATABASES` is reduced to its `ENGINE` per alias on the target's side, so the
password never crosses the pipe rather than crossing it and being dropped here.
Nothing in this list is a credential, and adding one would need a reason that
survives the paragraph at the top of this module.
"""

SCRIPT = """
import json, sys, runpy
import django

class _Intercepted(Exception):
    pass

def _stop(*args, **kwargs):
    raise _Intercepted()

import django.core.management as _management
_management.execute_from_command_line = _stop
try:
    runpy.run_path({manage!r}, run_name='__main__')
except (_Intercepted, SystemExit):
    pass

from django.conf import settings
settings.SETTINGS_MODULE

answers, problems = {{}}, {{}}
for name, expression in {questions!r}:
    try:
        answers[name] = eval(expression)
    except Exception as failure:
        problems[name] = '%s: %s' % (type(failure).__name__, failure)

sys.stdout.write({open!r} + json.dumps(
    {{'answers': answers, 'problems': problems}}, default=str
) + {close!r})
"""
"""Run the project's own `manage.py`, then stop it before it does any work.

The obvious route is `manage.py shell -c`, and it was written that way first.
It does not survive contact: `shell` loads the app registry *and* opens the
database backend, so a project whose `ENGINE` is Postgres cannot answer "what
Django version are you" unless `psycopg` happens to be installed and the server
happens to be up. That is precisely the CI case for every corpus we test
against, and none of the questions here needs a database.

So `manage.py` is executed under `runpy` with
`execute_from_command_line` replaced by something that raises. Everything the
project does on the way to that call still happens -- reading a `.env` file,
choosing between settings modules, `os.environ.setdefault` -- and nothing after
it does. We get the project's own settings resolution without its app registry,
which is the smallest thing that answers the question honestly.

Doing it this way rather than setting `DJANGO_SETTINGS_MODULE` ourselves is the
same refusal the runner makes: which settings module this project resolves is
the thing being measured, and supplying it would be measuring our own answer.

`settings.SETTINGS_MODULE` is touched before the questions begin, on purpose.
Django's settings are lazy, so without it a settings module that raises would
fail ten times over as ten confusing per-question problems instead of once, as
one traceback the reader can act on.
"""


class Unavailable(NamedTuple):
    """The live tier could not answer, and this is what to tell the user."""

    reason: str
    detail: str = ""

    @property
    def available(self) -> bool:
        return False

    def explain(self) -> str:
        return f"{self.reason}: {self.detail}" if self.detail else self.reason


class LiveContext(NamedTuple):
    """What the target's Django reported about itself."""

    interpreter: Interpreter
    django_version: str | None = None
    python_version: str | None = None
    settings_module: str | None = None
    debug: bool | None = None
    databases: tuple[tuple[str, str], ...] = ()
    installed_apps: tuple[str, ...] = ()
    use_tz: bool | None = None
    allowed_hosts: tuple[str, ...] = ()
    middleware: tuple[str, ...] = ()
    default_auto_field: str | None = None
    problems: tuple[str, ...] = ()

    @property
    def available(self) -> bool:
        return True

    @property
    def engines(self) -> frozenset[str]:
        """The distinct backends in use, for rules that only care which."""
        return frozenset(engine for _, engine in self.databases if engine)

    def uses(self, backend: str) -> bool:
        """Whether any alias is served by `backend`, e.g. `postgresql`.

        Matched on the last dotted segment so that a project routing through
        `django.contrib.gis.db.backends.postgis` or a vendored subclass is still
        recognised as Postgres.
        """
        return any(engine.rsplit(".", 1)[-1] == backend for engine in self.engines)

    def explain(self) -> str:
        parts = [
            f"Django {self.django_version} on Python {self.python_version}",
            f"settings {self.settings_module}",
        ]
        if self.databases:
            parts.append(", ".join(f"{alias} via {engine}" for alias, engine in self.databases))
        return "; ".join(parts)


def _payload(stdout: str) -> dict[str, Any] | None:
    """The framed JSON, or `None` if the target never got that far.

    Found between the delimiters rather than by parsing the whole stream,
    because the target may have written anything at all in front of it.
    """
    start = stdout.find(OPEN)
    end = stdout.find(CLOSE, start + 1)
    if start < 0 or end < 0:
        return None
    try:
        parsed = json.loads(stdout[start + len(OPEN) : end])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _summarise(outcome: Outcome) -> str:
    """The last thing the target said, for a user who has to act on it.

    A traceback's final line names the failure; the thousand lines above it name
    Django's import machinery, which the reader did not write and cannot fix.
    """
    tail = [line for line in outcome.stderr.splitlines() if line.strip()]
    return tail[-1].strip() if tail else outcome.describe()


def inspect_target(
    interpreter: Interpreter,
    manage_py: Path,
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> LiveContext | Unavailable:
    """Ask the target's Django what it is, via its own `manage.py`.

    Routed through the project's own `manage.py` because that is where
    `DJANGO_SETTINGS_MODULE` is chosen. Setting that variable ourselves would
    answer the question we are asking -- which settings this project actually
    resolves -- and the runner refuses to set it for the same reason. See
    `SCRIPT` for how the file is run without letting it do any work.
    """
    if not manage_py.is_file():
        return Unavailable("no manage.py", f"{manage_py} is not a file")

    script = SCRIPT.format(manage=manage_py.name, questions=QUESTIONS, open=OPEN, close=CLOSE)
    outcome = run_python(
        interpreter,
        ["-c", script],
        cwd=manage_py.parent,
        timeout=timeout,
    )

    payload = _payload(outcome.stdout)
    if payload is None:
        if outcome.timed_out:
            return Unavailable(
                "the target did not respond in time",
                f"its manage.py was killed after {timeout:g}s",
            )
        return Unavailable("the target's Django did not start", _summarise(outcome))

    answers = payload.get("answers", {})
    problems = payload.get("problems", {})
    if not isinstance(answers, dict) or not isinstance(problems, dict):
        return Unavailable("the target's reply was not in the expected shape")

    databases = answers.get("databases") or {}
    return LiveContext(
        interpreter=interpreter,
        django_version=answers.get("django_version"),
        python_version=answers.get("python_version"),
        settings_module=answers.get("settings_module"),
        debug=answers.get("debug"),
        databases=tuple(sorted((str(a), str(e)) for a, e in databases.items() if e)),
        installed_apps=tuple(answers.get("installed_apps") or ()),
        use_tz=answers.get("use_tz"),
        allowed_hosts=tuple(answers.get("allowed_hosts") or ()),
        middleware=tuple(answers.get("middleware") or ()),
        default_auto_field=answers.get("default_auto_field") or None,
        problems=tuple(f"{name}: {why}" for name, why in sorted(problems.items())),
    )

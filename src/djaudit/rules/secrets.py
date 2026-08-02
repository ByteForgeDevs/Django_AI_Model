"""DJS -- secret management.

A secret that resolves to a value we can read is a secret anyone with
repository access can read. These rules deliberately never quote the value they
found: a finding travels into JSON artifacts, SARIF uploads and CI logs, all of
which are shared more widely than the source it came from.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass

from djaudit.context import ProjectContext
from djaudit.models import Confidence, Evidence, EvidenceKind, Family, Finding, Severity, Tier
from djaudit.registry import RuleMeta, register
from djaudit.rules._base import (
    Entry,
    SettingGroup,
    SettingsRule,
    assignment_value,
    entries,
    literal_text,
)


@register
class HardcodedSecretKey(SettingsRule):
    """``SECRET_KEY`` resolves to a value that is readable in the source."""

    setting = "SECRET_KEY"
    ceiling = Confidence.CERTAIN

    meta = RuleMeta(
        id="DJS-002",
        title="SECRET_KEY is readable in the source",
        family=Family.DJS,
        severity=Severity.CRITICAL,
        confidence=Confidence.CERTAIN,
        tier=Tier.STATIC,
        rationale=(
            "SECRET_KEY signs session cookies, password reset tokens, and the CSRF "
            "token. Anyone who knows it can forge a session for any user, including "
            "staff, and mint password reset links -- no password required and nothing "
            "unusual in the logs. A key committed to the repository is known to every "
            "person who has ever had read access, every fork, and every CI provider "
            "that cached the checkout, and it stays known after they leave. Rotating "
            "it is the only remedy, and rotation logs everyone out, so the cost only "
            "grows."
        ),
        remediation=(
            "Generate a fresh key and load it from the environment:\n"
            '    python -c "from django.core.management.utils import '
            'get_random_secret_key; print(get_random_secret_key())"\n'
            "    SECRET_KEY = os.environ['DJANGO_SECRET_KEY']\n"
            "Subscripting rather than .get() makes a missing key fail at startup "
            "instead of silently falling back to the committed one. Treat the old "
            "key as compromised and rotate it, remembering that this logs every user "
            "out."
        ),
        references=(
            "https://docs.djangoproject.com/en/stable/ref/settings/#secret-key",
            "https://docs.djangoproject.com/en/stable/howto/deployment/checklist/#secret-key",
            "https://cwe.mitre.org/data/definitions/798.html",
        ),
    )

    def inspect(self, ctx: ProjectContext, group: SettingGroup) -> Iterator[Finding]:
        resolved = group.setting
        if not resolved.is_explicit:
            return

        secret = literal_text(resolved.value)
        # An empty key is a different defect: Django refuses to start, so it is
        # a broken deployment rather than a forgeable one.
        if not secret:
            return

        # A guessable key is everything this rule describes and worse, so
        # DJS-003 reports it instead. Two findings on one line would only make
        # the reader decide which of them to act on.
        if weakness(secret) is not None:
            return

        where = group.module.dotted or ctx.rel(group.module.path)
        how = (
            "as the fallback when the environment does not supply one"
            if resolved.value.env_dependent
            else "directly in the source"
        )

        yield self.report(
            ctx,
            group,
            message=(
                f"SECRET_KEY is set {how} in {where}{group.describe_reach()}, so anyone "
                f"who can read the repository can forge sessions and password reset tokens"
            ),
            evidence=(
                Evidence(
                    kind=EvidenceKind.CONFIG,
                    content=describe_secret(secret),
                    source="djaudit secret analysis",
                ),
            ),
            redact=secret,
        )


def describe_secret(secret: str) -> str:
    """Characterise a secret without disclosing it."""
    classes = {
        "lowercase": any(c.islower() for c in secret),
        "uppercase": any(c.isupper() for c in secret),
        "digits": any(c.isdigit() for c in secret),
        "symbols": any(not c.isalnum() for c in secret),
    }
    present = ", ".join(name for name, found in classes.items() if found) or "none"
    return (
        f"length={len(secret)}  distinct characters={len(set(secret))}  "
        f"character classes={present}\n"
        "value withheld: it is a live credential and a finding travels further "
        "than the source does"
    )


@register
class WeakSecretKey(SettingsRule):
    """``SECRET_KEY`` resolves to a value that does not need stealing."""

    setting = "SECRET_KEY"
    ceiling = Confidence.CERTAIN

    meta = RuleMeta(
        id="DJS-003",
        title="SECRET_KEY is guessable",
        family=Family.DJS,
        severity=Severity.CRITICAL,
        confidence=Confidence.CERTAIN,
        tier=Tier.STATIC,
        rationale=(
            "A key that is merely committed has to be found; a key that is a "
            "placeholder, a startproject default, or a dozen characters long does not. "
            "It is in a wordlist, and signing keys are attacked offline -- an attacker "
            "holding one session cookie can test candidate keys locally, as fast as "
            "their hardware allows, with nothing reaching the site to be rate-limited, "
            "locked out or logged. Success means forging a session for any user, staff "
            "included, and minting password reset links. Because guessing needs no "
            "access to the repository at all, this is worse than a strong key that "
            "leaked: closing the source down does not help."
        ),
        remediation=(
            "Generate a real key and load it from the environment:\n"
            '    python -c "from django.core.management.utils import '
            'get_random_secret_key; print(get_random_secret_key())"\n'
            "    SECRET_KEY = os.environ['DJANGO_SECRET_KEY']\n"
            "Subscripting rather than .get() makes a missing key stop the process at "
            "startup, which is what you want -- a fallback is how a placeholder reaches "
            "production in the first place. Assume anything signed with the old key is "
            "forgeable and rotate it; this logs every user out, which is the cheaper "
            "half of the problem."
        ),
        references=(
            "https://docs.djangoproject.com/en/stable/ref/settings/#secret-key",
            "https://docs.djangoproject.com/en/stable/howto/deployment/checklist/#secret-key",
            "https://cwe.mitre.org/data/definitions/1392.html",
        ),
    )

    def inspect(self, ctx: ProjectContext, group: SettingGroup) -> Iterator[Finding]:
        resolved = group.setting
        if not resolved.is_explicit:
            return

        secret = literal_text(resolved.value)
        if not secret:
            return

        found = weakness(secret)
        if found is None:
            return

        where = group.module.dotted or ctx.rel(group.module.path)
        how = "falls back to" if resolved.value.env_dependent else "is"

        yield self.report(
            ctx,
            group,
            message=(
                f"SECRET_KEY in {where}{group.describe_reach()} {how} a key that is "
                f"{found.kind}, so forging a session needs no access to the source"
            ),
            evidence=(
                Evidence(
                    kind=EvidenceKind.CONFIG,
                    content=f"{found.detail}\n\n{describe_secret(secret)}",
                    source="djaudit secret analysis",
                ),
            ),
            redact=secret,
        )


DJANGO_INSECURE_PREFIX = "django-insecure-"
"""``startproject`` writes this in front of the key it generates, precisely so
that a key which was never meant to leave a laptop can be recognised later."""

MIN_KEY_LENGTH = 32
"""Where a hex-encoded 128-bit key lands, and the floor below which a key is
short enough to be worth attacking directly.

``get_random_secret_key()`` produces 50 characters over a 50-character
alphabet, so anything Django generated clears this comfortably; so does
``secrets.token_hex(16)`` at exactly 32, and ``token_urlsafe(32)`` at 43. A key
below the line was written by a person rather than generated.
"""

MIN_DISTINCT = 6
"""Fewer distinct characters than this over a long value means a pattern.

A random key drawn from any realistic alphabet uses most of it; ``a`` repeated
sixty times is sixty characters of nothing. Length alone would pass it.
"""

_PLACEHOLDER_WORDS = frozenset(
    {
        "",  # value had no alphanumeric content at all, e.g. "---" or "..."
        "abc",
        "asdf",
        "bar",
        "baz",
        "dev",
        "dummy",
        "fake",
        "foo",
        "key",
        "none",
        "null",
        "qwerty",
        "secret",
        "test",
        "todo",
        "xxx",
    }
)
"""Matched only against the whole normalised value.

These are short enough to turn up inside a genuinely random key by chance, so
letting them match a substring would trade a real defect class for a stream of
nonsense on keys that are perfectly fine.
"""

_PLACEHOLDER_PHRASES = (
    "changeme",
    "changethis",
    "development",
    "djangosecretkey",
    "example",
    "insecure",
    "mysecretkey",
    "notasecret",
    "password",
    "placeholder",
    "replaceme",
    "s3cr3t",
    "secretkey",
    "supersecret",
    "testing",
    "topsecret",
    "yoursecretkey",
)
"""Matched anywhere in the normalised value.

At six characters and up the odds of one appearing inside a random key are
negligible -- roughly one in a billion for a six-letter phrase over Django's
alphabet -- while people really do write ``your-secret-key-here-change-me``.
"""


@dataclass(frozen=True, slots=True)
class Weakness:
    """Why a key we can read is also a key that can be guessed."""

    kind: str
    detail: str


def normalise(secret: str) -> str:
    """Reduce a value to its alphanumeric core, lowercased.

    ``CHANGE_ME!``, ``change-me`` and ``ChangeMe`` are the same placeholder
    wearing different punctuation, and ``---`` reduces to nothing at all.
    """
    return "".join(c for c in secret if c.isalnum()).lower()


def weakness(secret: str) -> Weakness | None:
    """Classify a readable key as guessable, or return ``None``.

    Ordered from most specific to least, so the reported reason is the most
    informative one that applies. A short placeholder would satisfy several of
    these, and "it is the string ``changeme``" tells a reader more than "it is
    under 32 characters".
    """
    if secret.startswith(DJANGO_INSECURE_PREFIX):
        return Weakness(
            "generated by startproject for local development",
            f"the value carries Django's own {DJANGO_INSECURE_PREFIX!r} marker, which "
            "startproject attaches to the throwaway key it writes into a new project. "
            "It was never meant to reach a deployment, and it has been sitting in "
            "version control since the first commit.",
        )

    core = normalise(secret)
    if core in _PLACEHOLDER_WORDS:
        return Weakness(
            "a placeholder that was never replaced",
            "ignoring case and punctuation, the value is one of the handful of stand-in "
            "strings people type when they intend to come back to it. Anyone attacking "
            "the site tries these first, and they cost nothing to try.",
        )
    for phrase in _PLACEHOLDER_PHRASES:
        if phrase in core:
            return Weakness(
                "a placeholder that was never replaced",
                "ignoring case and punctuation, the value contains a well-known stand-in "
                "phrase. Anyone attacking the site tries these first, and they cost "
                "nothing to try.",
            )

    if len(secret) < MIN_KEY_LENGTH:
        return Weakness(
            "too short to resist being guessed",
            f"the value is {len(secret)} characters, below the {MIN_KEY_LENGTH} where a "
            "hex-encoded 128-bit key sits. Django generates 50 characters over a "
            "50-character alphabet; a value this short was chosen by a person, and "
            "signing keys are attacked offline, where guesses are free and unlimited.",
        )

    distinct = len(set(secret))
    if distinct < MIN_DISTINCT:
        return Weakness(
            "long but patterned",
            f"the value is {len(secret)} characters drawn from only {distinct} distinct "
            "ones, so its length is padding rather than entropy. A search only has to "
            "cover the pattern, not the length.",
        )
    return None


@register
class HardcodedDatabasePassword(SettingsRule):
    """A ``DATABASES`` alias carries a password readable in the source."""

    setting = "DATABASES"
    ceiling = Confidence.CERTAIN

    meta = RuleMeta(
        id="DJS-004",
        title="database password is readable in the source",
        family=Family.DJS,
        severity=Severity.CRITICAL,
        confidence=Confidence.CERTAIN,
        tier=Tier.STATIC,
        rationale=(
            "A database password in the repository is known to everyone who has ever "
            "had read access, every fork, and every CI provider that cached the "
            "checkout, and it stays known after they leave. Unlike a signing key it "
            "usually cannot be rotated quietly -- the credential is shared by every "
            "process that talks to the database, so changing it is a coordinated "
            "restart, which is exactly the pressure that leaves it unrotated for "
            "years. Whoever has it can read and change every row: users, sessions, "
            "audit tables. If the database is reachable from outside the deployment "
            "network, that is the whole application."
        ),
        remediation=(
            "Read the credential from the environment or a secret manager, and let a "
            "missing one stop the process:\n"
            "    'PASSWORD': os.environ['DB_PASSWORD']\n"
            "Rotate the exposed password, and remember that removing it from the "
            "current source does not remove it from the history, so treat it as "
            "compromised regardless. If the database is only reachable inside the "
            "deployment network, that limits the damage but does not repair it."
        ),
        references=(
            "https://docs.djangoproject.com/en/stable/ref/settings/#databases",
            "https://cwe.mitre.org/data/definitions/798.html",
            "https://owasp.org/Top10/A05_2021-Security_Misconfiguration/",
        ),
    )

    def inspect(self, ctx: ProjectContext, group: SettingGroup) -> Iterator[Finding]:
        if not group.setting.is_assigned:
            return

        view = self.views[group.module.dotted]
        setting = group.setting
        for alias, entry in entries(view, assignment_value(setting), setting.value).items():
            # Each alias is a separate credential on a separate server, so each
            # is its own finding rather than one lumped together.
            config = entries(view, entry.node, entry.value)
            password = config.get("PASSWORD")
            if password is None:
                continue

            secret = literal_text(password.value)
            if not secret:
                # Django reads an empty password as "no password supplied",
                # which is a connection problem rather than a disclosure.
                continue

            where = group.module.dotted or ctx.rel(group.module.path)
            how = (
                "falls back to a password written into"
                if password.value.env_dependent
                else "takes its password from"
            )

            yield self.report(
                ctx,
                group.narrow(f'DATABASES["{alias}"]["PASSWORD"]', password.value),
                at=password.node,
                message=(
                    f"the {alias!r} database in {where}{group.describe_reach()} {how} "
                    f"the source, so anyone who can read the repository can connect to it"
                ),
                evidence=(
                    Evidence(
                        kind=EvidenceKind.CONFIG,
                        content=f"alias={alias}  {_engine(config)}\n{describe_secret(secret)}",
                        source="djaudit secret analysis",
                    ),
                ),
                redact=secret,
            )


def _engine(config: dict[str, Entry]) -> str:
    """Name the backend, which decides how much a leaked password is worth."""
    engine = config.get("ENGINE")
    name = literal_text(engine.value) if engine else None
    return f"engine={name}" if name else "engine=unresolved"


_SECRET_TAIL = frozenset({"SECRET", "PASSWORD", "PASSWD", "TOKEN", "CREDENTIALS", "APIKEY"})
"""A single trailing word that makes a setting a credential.

Matched against the last underscore-separated word only. `PASSWORD_HASHERS`
and `AUTH_PASSWORD_VALIDATORS` both contain `PASSWORD` and neither holds one;
what they hold is policy. Requiring the word to end the name separates the two
without a list of exceptions to maintain.
"""

_SECRET_PAIR = frozenset(
    {
        ("ACCESS", "KEY"),
        ("ACCESS", "TOKEN"),
        ("API", "KEY"),
        ("AUTH", "TOKEN"),
        ("CLIENT", "SECRET"),
        ("ENCRYPTION", "KEY"),
        ("PRIVATE", "KEY"),
        ("REFRESH", "TOKEN"),
        ("SECRET", "KEY"),
        ("SIGNING", "KEY"),
    }
)
"""Two trailing words, for the cases where `KEY` alone would be far too broad.

`CACHE_KEY_PREFIX`, `SORT_KEY` and healthchecks' own `TRELLO_APP_KEY` are
ordinary configuration. `S3_ACCESS_KEY` is not. The distinction is the word in
front, so that is what gets matched.
"""

_KEY_MATERIAL = frozenset({("ENCRYPTION", "KEY"), ("PRIVATE", "KEY"), ("SIGNING", "KEY")})
"""Credentials that sign or decrypt, rather than authenticating to one service.

A leaked service token is bounded by that service's permissions and can be
revoked there. Key material compromises everything it ever protected, including
data already at rest, and revoking it does not undo that.
"""

_IMPORT_PATH = re.compile(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)+$")


def looks_like_a_secret_name(setting: str) -> bool:
    """Whether the setting's name says it holds a credential."""
    if setting == "SECRET_KEY":
        # DJS-002 and DJS-003 own this one, and say more about it.
        return False
    words = setting.split("_")
    if words[-1] in _SECRET_TAIL:
        return True
    return len(words) >= 2 and (words[-2], words[-1]) in _SECRET_PAIR


@register
class HardcodedServiceCredential(SettingsRule):
    """A setting named as a credential holds one, in the source."""

    ceiling = Confidence.CERTAIN

    meta = RuleMeta(
        id="DJS-005",
        title="service credential is readable in the source",
        family=Family.DJS,
        severity=Severity.HIGH,
        confidence=Confidence.CERTAIN,
        tier=Tier.STATIC,
        rationale=(
            "A credential in the repository is known to everyone who has ever had read "
            "access, every fork, and every CI provider that cached the checkout, and it "
            "stays known after they leave. Removing it from the current source does not "
            "remove it from the history. What it costs depends on what it opens -- a "
            "payment provider key moves money, an object-store key exposes every file, "
            "a mail credential lets someone send as you -- but in each case the holder "
            "is acting as your application, which is what makes it hard to notice."
        ),
        remediation=(
            "Read the credential from the environment or a secret manager, and let a "
            "missing one stop the process:\n"
            "    STRIPE_SECRET_KEY = os.environ['STRIPE_SECRET_KEY']\n"
            "Then revoke the exposed credential at the provider and issue a new one. "
            "Revoking matters more than rewriting history: the old value is already "
            "out, and most providers show when a key was last used, which is worth "
            "checking before you assume nobody found it."
        ),
        references=(
            "https://cwe.mitre.org/data/definitions/798.html",
            "https://owasp.org/Top10/A05_2021-Security_Misconfiguration/",
            "https://docs.djangoproject.com/en/stable/topics/settings/",
        ),
    )

    def selects(self, name: str) -> bool:
        return looks_like_a_secret_name(name)

    def inspect(self, ctx: ProjectContext, group: SettingGroup) -> Iterator[Finding]:
        resolved = group.setting
        if not resolved.is_explicit:
            return

        secret = literal_text(resolved.value)
        if not secret:
            # An unset credential is a deployment that has not been finished,
            # not a disclosure.
            return
        if _IMPORT_PATH.match(secret):
            # `FOO_TOKEN = "myapp.tokens.Backend"` names a class to import.
            return

        where = group.module.dotted or ctx.rel(group.module.path)
        how = "falls back to a value written into" if resolved.value.env_dependent else "is set in"

        yield self.report(
            ctx,
            group,
            message=(
                f"{resolved.name}, whose name says it holds a credential, {how} "
                f"{where}{group.describe_reach()}, so anyone who can read the "
                f"repository can use it"
            ),
            severity=_severity(resolved.name),
            evidence=(
                Evidence(
                    kind=EvidenceKind.CONFIG,
                    content=describe_secret(secret),
                    source="djaudit secret analysis",
                ),
            ),
            redact=secret,
        )


def _severity(setting: str) -> Severity:
    words = setting.split("_")
    return (
        Severity.CRITICAL
        if len(words) >= 2 and (words[-2], words[-1]) in _KEY_MATERIAL
        else Severity.HIGH
    )

"""DJS -- password storage and password policy.

These two settings decide what a stolen database is worth. Everything else in
the DJS family is about keeping an attacker out; this is about what they get
when they are already in, which is the case that actually ends up in the news.
"""

from __future__ import annotations

from djaudit.models import Confidence, Family, Severity, Tier
from djaudit.registry import RuleMeta, register
from djaudit.rules._base import InsecureDefaultRule, entries_of
from djaudit.settings import ResolvedSetting
from djaudit.values import Value

_HASHERS_DOCS = "https://docs.djangoproject.com/en/stable/topics/auth/passwords/"

WEAK_HASHERS: frozenset[str] = frozenset(
    {
        "MD5PasswordHasher",
        "UnsaltedMD5PasswordHasher",
        "SHA1PasswordHasher",
        "UnsaltedSHA1PasswordHasher",
        "CryptPasswordHasher",
    }
)
"""Hashers that are one cheap pass over the password.

The last four were removed in Django 5.1, which is outside our support floor --
they are still listed because a settings module naming one is describing what
it *did* store, and because reading a repository is not the same as running it.
``PBKDF2SHA1PasswordHasher`` is deliberately absent: SHA1 there is the PRF
inside PBKDF2, iterated hundreds of thousands of times, which is not the same
thing as hashing a password with SHA1.
"""


def hasher_name(entry: object) -> str | None:
    """The class name at the end of a dotted hasher path."""
    if not isinstance(entry, str) or not entry:
        return None
    return entry.rsplit(".", 1)[-1]


def leading_hashers(value: Value) -> tuple[str, ...]:
    """The first hasher of every branch we could read.

    Only the first matters. Django hashes with ``PASSWORD_HASHERS[0]`` and
    keeps the rest solely to verify hashes that already exist.
    """
    found = []
    for entries in entries_of(value):
        if not entries:
            continue
        name = hasher_name(entries[0])
        if name is not None:
            found.append(name)
    return tuple(found)


@register
class WeakPasswordHasher(InsecureDefaultRule):
    """The first entry of ``PASSWORD_HASHERS`` is a fast hash.

    Reporting only the first entry is the whole design of this rule. Django's
    documented way to move off a weak hasher is to leave it further down the
    list so existing users can still log in, at which point Django silently
    re-hashes their password with the new one. A rule that flagged a weak
    hasher anywhere would therefore flag the correct migration, and a tool that
    complains about the fix teaches people to stop reading it.

    The cost is a real blind spot: an account that has not logged in since the
    migration still holds the old hash, and we say nothing about it. That is
    recorded here rather than papered over.
    """

    setting = "PASSWORD_HASHERS"
    ceiling = Confidence.CERTAIN
    """The list says what it says. There is no proxy, no middleware and no
    environment between this value and what Django writes to the database."""

    corrected_as = "puts a slow hasher first"

    def insecure(self, value: Value) -> bool:
        leading = leading_hashers(value)
        return bool(leading) and any(name in WEAK_HASHERS for name in leading)

    def describe_state(self, resolved: ResolvedSetting) -> str:
        weak = [name for name in leading_hashers(resolved.value) if name in WEAK_HASHERS]
        return f"hashes new passwords with {' or '.join(sorted(set(weak)))}"

    def consequence_for(self, resolved: ResolvedSetting) -> str:
        return (
            "a stolen database is a list of plaintext passwords rather than a wall an "
            "attacker gives up on -- these are single-pass hashes built to be fast, and "
            "commodity hardware tries billions of candidates a second against them, so "
            "the whole user table falls in hours and every password reused elsewhere "
            "falls with it"
        )

    meta = RuleMeta(
        id="DJS-019",
        title="passwords are hashed with a fast algorithm",
        family=Family.DJS,
        severity=Severity.HIGH,
        confidence=Confidence.CERTAIN,
        tier=Tier.STATIC,
        rationale=(
            "Django hashes every new password with PASSWORD_HASHERS[0] and keeps the "
            "remaining entries only to verify hashes that already exist. A password "
            "hasher is supposed to be slow: that is its entire security property, "
            "because the attacker's advantage in an offline attack is throughput. MD5 "
            "and SHA1 are built for the opposite goal, and salting them fixes only "
            "rainbow tables, not brute force. Django ships a safe default -- PBKDF2 "
            "with a very large iteration count -- so this setting is only ever weak "
            "because someone chose to make it weak, usually to speed up a test suite "
            "in a file that turned out to reach production."
        ),
        remediation=(
            "Put a deliberately slow hasher first: Argon2 (`pip install "
            "django[argon2]`) is the strongest option Django ships, and the default "
            "PBKDF2PasswordHasher is a perfectly good second choice that needs no "
            "extra dependency. Leave the weak hasher further down the list rather "
            "than deleting it, so existing users can still log in -- Django re-hashes "
            "each password with the new leading hasher the next time it verifies one. "
            "If the goal was a faster test suite, override PASSWORD_HASHERS in the "
            "test settings module only, never in one production can import."
        ),
        references=(
            _HASHERS_DOCS,
            "https://docs.djangoproject.com/en/stable/topics/auth/passwords/#how-django-stores-passwords",
            "https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html",
        ),
    )

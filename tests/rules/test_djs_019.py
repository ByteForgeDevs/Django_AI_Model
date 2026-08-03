"""DJS-019 -- the first entry of PASSWORD_HASHERS is a fast hash."""

from __future__ import annotations

import pathlib

import pytest

from djaudit import engine, registry
from djaudit.models import Finding
from djaudit.rules.auth import WEAK_HASHERS, hasher_name, leading_hashers
from djaudit.values import Value

MARKERS = "INSTALLED_APPS = []\nDATABASES = {}\nSECRET_KEY = 'x'\nDEBUG = False\n"

PBKDF2 = "django.contrib.auth.hashers.PBKDF2PasswordHasher"
MD5 = "django.contrib.auth.hashers.MD5PasswordHasher"
ARGON2 = "django.contrib.auth.hashers.Argon2PasswordHasher"


def build(tmp_path: pathlib.Path, body: str) -> pathlib.Path:
    root = tmp_path / "proj"
    (root / "config").mkdir(parents=True)
    (root / "manage.py").write_text(
        "import os\nos.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')\n"
    )
    (root / "config" / "__init__.py").write_text("")
    (root / "config" / "settings.py").write_text(MARKERS + body)
    return root


def findings(root: pathlib.Path) -> list[Finding]:
    report = engine.run(root)
    return [f for f in report.findings if f.rule_id == "DJS-019"]


def test_hasher_name_takes_the_last_component() -> None:
    assert hasher_name(MD5) == "MD5PasswordHasher"
    assert hasher_name("MD5PasswordHasher") == "MD5PasswordHasher"


@pytest.mark.parametrize("entry", ["", None, 3, ["nested"]])
def test_hasher_name_ignores_anything_that_is_not_a_path(entry: object) -> None:
    assert hasher_name(entry) is None


def test_leading_hashers_reads_every_branch() -> None:
    value = Value.conditional((Value.of([MD5, PBKDF2]), Value.of([ARGON2])))
    assert leading_hashers(value) == ("MD5PasswordHasher", "Argon2PasswordHasher")


def test_leading_hashers_skips_an_empty_list() -> None:
    assert leading_hashers(Value.of([])) == ()


def test_leading_hashers_gives_up_on_an_unreadable_value() -> None:
    assert leading_hashers(Value.unknown("built by a helper")) == ()


def test_weak_first_is_reported(tmp_path: pathlib.Path) -> None:
    found = findings(build(tmp_path, f"PASSWORD_HASHERS = [{MD5!r}, {PBKDF2!r}]\n"))
    assert len(found) == 1
    assert found[0].severity.value == "high"
    assert found[0].confidence.value == "certain"
    assert "MD5PasswordHasher" in found[0].message


def test_weak_below_a_strong_hasher_is_the_migration_path_and_stays_silent(
    tmp_path: pathlib.Path,
) -> None:
    # Django re-hashes on the next successful login, so this list is a project
    # moving off MD5 correctly. Reporting it would report the fix.
    assert findings(build(tmp_path, f"PASSWORD_HASHERS = [{PBKDF2!r}, {MD5!r}]\n")) == []


def test_the_django_default_stays_silent(tmp_path: pathlib.Path) -> None:
    assert findings(build(tmp_path, "")) == []


def test_a_strong_explicit_list_stays_silent(tmp_path: pathlib.Path) -> None:
    assert findings(build(tmp_path, f"PASSWORD_HASHERS = [{ARGON2!r}, {PBKDF2!r}]\n")) == []


def test_pbkdf2_sha1_is_not_weak(tmp_path: pathlib.Path) -> None:
    # SHA1 there is the PRF inside PBKDF2, iterated hundreds of thousands of
    # times. Matching on the substring rather than the class name would have
    # made this a false positive, and it appears in real settings modules.
    sha1 = "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher"
    assert findings(build(tmp_path, f"PASSWORD_HASHERS = [{sha1!r}]\n")) == []


def test_an_unreadable_list_stays_silent(tmp_path: pathlib.Path) -> None:
    assert findings(build(tmp_path, "PASSWORD_HASHERS = build_hashers()\n")) == []


def test_an_empty_list_stays_silent(tmp_path: pathlib.Path) -> None:
    # Django raises at startup for this, so it is a broken deployment rather
    # than a weak one, and DJS-019 is not the rule that should say so.
    assert findings(build(tmp_path, "PASSWORD_HASHERS = []\n")) == []


def test_a_branch_that_is_weak_is_enough(tmp_path: pathlib.Path) -> None:
    body = (
        "import os\n"
        "if os.environ.get('FAST_TESTS'):\n"
        f"    PASSWORD_HASHERS = [{MD5!r}]\n"
        "else:\n"
        f"    PASSWORD_HASHERS = [{ARGON2!r}]\n"
    )
    found = findings(build(tmp_path, body))
    assert len(found) == 1
    assert found[0].confidence.value != "certain"


def test_every_removed_hasher_is_still_recognised(tmp_path: pathlib.Path) -> None:
    # Django dropped these in 5.1, but a settings module naming one describes
    # what the database already holds.
    for name in sorted(WEAK_HASHERS):
        path = f"django.contrib.auth.hashers.{name}"
        assert findings(build(tmp_path / name, f"PASSWORD_HASHERS = [{path!r}]\n")), name


def test_the_rule_is_registered() -> None:
    # all_rules() loads the built-in catalogue on first call.
    assert any(rule.meta.id == "DJS-019" for rule in registry.all_rules())

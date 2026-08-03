"""Reading dependency manifests.

The classification these tests pin down is the one thing DJS-025 cannot get
wrong in either direction: too permissive and it is silent on real deployments,
too strict and it reports every project that has correctly separated its
tooling.
"""

from __future__ import annotations

import pathlib

import pytest

from djaudit.manifest import (
    deployed,
    discover,
    is_development,
    normalise,
    read_requirements,
    requirement_name,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("django-debug-toolbar==7.0.0", "django-debug-toolbar"),
        ("Django[argon2]==6.0.7", "django"),
        ("Django_Debug_Toolbar", "django-debug-toolbar"),
        ("psycopg[binary]==3.3.4", "psycopg"),
        ("silk>=5,<6  # profiling", "silk"),
        ('foo ; python_version < "3.13"', "foo"),
        ("pkg @ git+https://example.test/pkg.git", "pkg"),
        ("-r base.txt", None),
        ("--index-url https://example.test/simple", None),
        ("-e .", None),
        ("# a comment", None),
        ("", None),
        ("   ", None),
        ("https://example.test/pkg.whl", None),
    ],
)
def test_requirement_lines_are_read(raw: str, expected: str | None) -> None:
    assert requirement_name(raw) == expected


def test_names_are_compared_the_way_pep_503_says() -> None:
    assert normalise("Django_Debug.Toolbar") == normalise("django-debug-toolbar")


@pytest.mark.parametrize(
    "stem",
    ["requirements-dev", "dev-requirements", "dev", "requirements.test", "local", "ci"],
)
def test_development_manifests_are_recognised(stem: str) -> None:
    assert is_development(stem)


@pytest.mark.parametrize(
    "stem",
    ["requirements", "base_requirements", "production", "base", "requirements-prod"],
)
def test_production_manifests_are_not(stem: str) -> None:
    # base_requirements.txt is NetBox's, and treating it as development because
    # it contains the word "base" would silence the rule on a real project.
    assert not is_development(stem)


def test_a_continued_line_keeps_its_first_position(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "requirements.txt"
    path.write_text("silk==5.4.0 \\\n    --hash=sha256:abc\n")
    found = read_requirements(path)
    assert [(r.name, r.line) for r in found] == [("silk", 1)]


def test_an_unreadable_file_yields_nothing(tmp_path: pathlib.Path) -> None:
    assert read_requirements(tmp_path / "missing.txt") == ()


def test_pyproject_groups_are_classified(tmp_path: pathlib.Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\n"
        'dependencies = ["django==6.0.7"]\n'
        "[project.optional-dependencies]\n"
        'ldap = ["django-auth-ldap"]\n'
        'dev = ["django-debug-toolbar"]\n'
        "[dependency-groups]\n"
        'test = ["pytest"]\n'
    )
    by_group = {m.group: m for m in discover(tmp_path)}
    assert by_group[""].development is False
    assert by_group["ldap"].development is False
    assert by_group["dev"].development is True
    assert by_group["test"].development is True


def test_poetry_tables_are_read(tmp_path: pathlib.Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.poetry.dependencies]\n"
        'python = "^3.13"\n'
        'silk = "*"\n'
        "[tool.poetry.group.dev.dependencies]\n"
        'django-debug-toolbar = "*"\n'
    )
    manifests = discover(tmp_path)
    main = next(m for m in manifests if not m.group)
    assert [r.name for r in main.requirements] == ["silk"]
    assert next(m for m in manifests if m.group == "dev").development


def test_pipfile_sections_are_read(tmp_path: pathlib.Path) -> None:
    (tmp_path / "Pipfile").write_text(
        '[packages]\ndjango = "*"\n[dev-packages]\ndjango-debug-toolbar = "*"\n'
    )
    manifests = {m.group: m for m in discover(tmp_path)}
    assert [r.name for r in manifests[""].requirements] == ["django"]
    assert manifests["dev-packages"].development


def test_broken_toml_is_skipped(tmp_path: pathlib.Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project\nnope")
    assert discover(tmp_path) == ()


def test_a_development_manifest_is_not_deployment(tmp_path: pathlib.Path) -> None:
    (tmp_path / "requirements.txt").write_text("django==6.0.7\n")
    (tmp_path / "requirements-dev.txt").write_text("django-debug-toolbar==7.0.0\n")
    manifests = discover(tmp_path)
    assert deployed(manifests, "django-debug-toolbar") is None
    assert deployed(manifests, "django") is not None


def test_the_source_manifest_is_preferred_over_the_compiled_one(
    tmp_path: pathlib.Path,
) -> None:
    # The fix belongs in the file a human edits; editing the pip-compile output
    # is how a dependency comes back on the next run.
    (tmp_path / "requirements.in").write_text("silk\n")
    (tmp_path / "requirements.txt").write_text("silk==5.4.0\n")
    found = deployed(discover(tmp_path), "django-silk")
    assert found is None
    found = deployed(discover(tmp_path), "silk")
    assert found is not None
    assert found[0].path.name == "requirements.in"


def test_vendored_directories_are_skipped(tmp_path: pathlib.Path) -> None:
    vendored = tmp_path / ".venv" / "lib"
    vendored.mkdir(parents=True)
    (vendored / "requirements.txt").write_text("silk\n")
    assert discover(tmp_path) == ()

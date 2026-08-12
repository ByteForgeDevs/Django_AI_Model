"""What djaudit is allowed to make a user install.

djaudit is pointed at other people's repositories, often in CI, often in an
image someone else pays to build. Every runtime dependency is a cost imposed on
them and a supply-chain surface imposed on us, so the list is short on purpose
and this file is what keeps it short. A dependency added without a deliberate
change here fails the build.

The trigger for writing it was substep 6.4.1, which was planned as "`libcst`
integration for structure-preserving rewrites". Measuring first showed a range
derived from `ast` positions re-parses to an identical tree for 99.790% of the
86,783 assignments in the benchmark corpus, and that the remaining 0.21% is
*detectable from inside* -- so `llm/edit.py` refuses those rather than
corrupting them. The dependency would have bought 0.21% of coverage, cost more
bytes than everything djaudit currently ships, and would not have supplied the
round-trip proof that makes an edit publishable. So it was not taken on, and
this file records that as a checked fact rather than a paragraph.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PYPROJECT = REPO / "pyproject.toml"
SOURCE = REPO / "src" / "djaudit"

# A CLI needs an argument parser and a terminal writer. Everything else djaudit
# does -- parsing, dataflow, the model graph, SARIF -- is stdlib.
ALLOWED_RUNTIME = {"typer", "rich"}

# Imported by tests only, and named here so a future runtime import of Django is
# an obvious failure rather than a quiet one. djaudit never imports the target's
# framework: it reads source, it does not load it.
BANNED_AT_RUNTIME = {"django", "rest_framework", "libcst", "requests", "httpx", "openai"}


def config() -> dict[str, object]:
    with PYPROJECT.open("rb") as handle:
        return tomllib.load(handle)


def declared_runtime() -> set[str]:
    project = config()["project"]
    assert isinstance(project, dict)
    raw = project.get("dependencies", [])
    assert isinstance(raw, list)
    return {str(item).split(">")[0].split("=")[0].split("[")[0].strip() for item in raw}


def top_level_imports() -> set[str]:
    """Every module name `src/djaudit` imports, at any depth."""
    names: set[str] = set()
    for path in SOURCE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return names


class TestRuntimeDependencies:
    def test_the_declared_list_is_exactly_what_we_allow(self) -> None:
        assert declared_runtime() == ALLOWED_RUNTIME

    def test_nothing_heavy_is_imported_at_runtime(self) -> None:
        assert top_level_imports() & BANNED_AT_RUNTIME == set()

    def test_libcst_is_not_a_dependency_declared_or_optional(self) -> None:
        """6.4.1 measured its way out of this one; keep it measured out."""
        project = config()["project"]
        assert isinstance(project, dict)
        extras = project.get("optional-dependencies", {})
        text = str(extras) + str(declared_runtime())

        assert "libcst" not in text

    def test_every_third_party_import_is_declared(self) -> None:
        """The direction the other tests do not cover: using something unlisted.

        stdlib is filtered by asking Python what stdlib is, rather than by a
        list here that would go stale on the next release.
        """
        import sys

        third_party = {
            name
            for name in top_level_imports()
            if name not in sys.stdlib_module_names and name != "djaudit"
        }

        assert third_party <= ALLOWED_RUNTIME

    def test_the_allow_list_is_not_vacuous(self) -> None:
        """A test suite that allowed everything would pass all of the above."""
        assert ALLOWED_RUNTIME & top_level_imports() == ALLOWED_RUNTIME


class TestScriptsAreNamedOnce:
    """`scripts/` is on mypy's `files` list, so it must never also be a package.

    The trigger: two test modules were written as `from scripts import x` while
    twelve others used `sys.path` plus a bare import. mypy then saw the same
    file under two module names, reported `Source file found twice`, and -- the
    part that matters -- stopped. `errors prevented further checking` means the
    whole type check silently became a no-op, and two commits went out with no
    types checked at all. CI would have caught it; CI was blocked on billing.
    """

    def test_scripts_is_not_a_package(self) -> None:
        assert not (REPO / "scripts" / "__init__.py").exists()

    def test_no_test_imports_scripts_as_a_package(self) -> None:
        offenders = []
        for path in sorted((REPO / "tests").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.ImportFrom)
                    and (node.module or "").split(".")[0] == "scripts"
                ):
                    offenders.append(f"{path.name}:{node.lineno}")
                if isinstance(node, ast.Import):
                    offenders += [
                        f"{path.name}:{node.lineno}"
                        for a in node.names
                        if a.name.split(".")[0] == "scripts"
                    ]
        assert not offenders, (
            f"import via sys.path instead, or mypy names the file twice: {offenders}"
        )

    def test_the_sys_path_idiom_actually_reaches_the_scripts(self) -> None:
        """The presence control: the alternative the previous test demands works."""
        importers = [
            p
            for p in sorted((REPO / "tests").rglob("*.py"))
            if 'sys.path.insert(0, str(ROOT / "scripts"))' in p.read_text(encoding="utf-8")
        ]
        assert len(importers) > 10, importers

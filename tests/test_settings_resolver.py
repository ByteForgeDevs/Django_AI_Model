"""Tests for settings resolution across split-settings inheritance.

The fixtures mirror the layout that actually breaks naive analysers: a base
module that enables DEBUG and a production module that turns it back off.
"""

from __future__ import annotations

from pathlib import Path

from djaudit.context import ProjectContext, SettingsModule, SettingsRole
from djaudit.discovery import build_context
from djaudit.settings import Origin, resolve_all, resolve_settings


def project(tmp_path: Path, files: dict[str, str]) -> ProjectContext:
    """Write a synthetic project and discover it the way a real run does."""
    for name, source in files.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source)
    return build_context(tmp_path)


def view(ctx: ProjectContext, dotted: str):
    return resolve_all(ctx)[dotted]


class TestInheritance:
    def test_production_overrides_base(self, overridden_project: Path) -> None:
        ctx = build_context(overridden_project)
        resolved = view(ctx, "config.settings.production").get("DEBUG")
        assert resolved.value.literal is False
        assert resolved.origin is Origin.EXPLICIT

    def test_the_override_chain_is_kept(self, overridden_project: Path) -> None:
        ctx = build_context(overridden_project)
        resolved = view(ctx, "config.settings.production").get("DEBUG")
        assert len(resolved.definitions) == 2
        assert resolved.definitions[0].dotted.endswith("base")
        assert resolved.definitions[1].dotted.endswith("production")
        assert [d.dotted for d in resolved.overridden] == [resolved.definitions[0].dotted]

    def test_base_alone_still_reads_true(self, overridden_project: Path) -> None:
        # Resolution is per-module: base.py on its own really does set DEBUG on.
        ctx = build_context(overridden_project)
        assert view(ctx, "config.settings.base").get("DEBUG").value.literal is True

    def test_development_also_inherits(self, overridden_project: Path) -> None:
        ctx = build_context(overridden_project)
        resolved = view(ctx, "config.settings.development")
        assert resolved.get("DEBUG").value.literal is True
        assert resolved.get("ALLOWED_HOSTS").value.literal == ["localhost", "127.0.0.1"]

    def test_inherited_settings_are_visible(self, overridden_project: Path) -> None:
        ctx = build_context(overridden_project)
        resolved = view(ctx, "config.settings.production")
        assert resolved.get("ROOT_URLCONF").value.literal == "config.urls"
        assert "django.contrib.admin" in resolved.get("INSTALLED_APPS").value.literal

    def test_the_chain_lists_modules_in_resolution_order(self, overridden_project: Path) -> None:
        ctx = build_context(overridden_project)
        chain = view(ctx, "config.settings.production").chain
        assert chain[-1].endswith("production")
        assert any(c.endswith("base") for c in chain[:-1])

    def test_a_module_with_no_star_import_has_a_chain_of_one(
        self, overridden_project: Path
    ) -> None:
        ctx = build_context(overridden_project)
        assert len(view(ctx, "config.settings.base").chain) == 1


class TestRelativeImports:
    def test_single_dot(self, tmp_path: Path) -> None:
        ctx = project(
            tmp_path,
            {
                "conf/__init__.py": "",
                "conf/base.py": "DEBUG = True\nSECRET_KEY = 'x'\nINSTALLED_APPS = []\n",
                "conf/prod.py": "from .base import *\nDEBUG = False\n",
            },
        )
        assert view(ctx, "conf.prod").get("DEBUG").value.literal is False

    def test_double_dot_climbs_a_package(self, tmp_path: Path) -> None:
        ctx = project(
            tmp_path,
            {
                "conf/__init__.py": "",
                "conf/shared.py": "DEBUG = True\nSECRET_KEY = 'x'\nINSTALLED_APPS = []\n",
                "conf/settings/__init__.py": "",
                "conf/settings/prod.py": "from ..shared import *\nDEBUG = False\n",
            },
        )
        assert view(ctx, "conf.settings.prod").get("DEBUG").value.literal is False

    def test_absolute_import(self, tmp_path: Path) -> None:
        ctx = project(
            tmp_path,
            {
                "conf/__init__.py": "",
                "conf/base.py": "DEBUG = True\nSECRET_KEY = 'x'\nINSTALLED_APPS = []\n",
                "conf/prod.py": "from conf.base import *\nDEBUG = False\n",
            },
        )
        assert view(ctx, "conf.prod").get("DEBUG").value.literal is False

    def test_a_missing_import_target_does_not_crash(self, tmp_path: Path) -> None:
        ctx = project(
            tmp_path,
            {
                "conf/__init__.py": "",
                "conf/settings.py": "from .nowhere import *\nDEBUG = True\nSECRET_KEY = 'x'\n",
            },
        )
        assert view(ctx, "conf.settings").get("DEBUG").value.literal is True

    def test_a_cycle_terminates(self, tmp_path: Path) -> None:
        ctx = project(
            tmp_path,
            {
                "conf/__init__.py": "",
                "conf/a.py": "from .b import *\nDEBUG = True\nSECRET_KEY = 'x'\n",
                "conf/b.py": "from .a import *\nINSTALLED_APPS = []\n",
            },
        )
        assert view(ctx, "conf.a").get("DEBUG").value.literal is True


class TestExpressionsAcrossModules:
    def test_a_helper_defined_in_base_is_usable_downstream(self, tmp_path: Path) -> None:
        # `from base import *` copies base's functions, so following the helper
        # from production is faithful rather than a shortcut.
        ctx = project(
            tmp_path,
            {
                "conf/__init__.py": "",
                "conf/base.py": (
                    "import os\n"
                    "def envbool(name, default):\n"
                    "    return os.getenv(name, default) == 'True'\n"
                    "DEBUG = False\nSECRET_KEY = 'x'\nINSTALLED_APPS = []\n"
                ),
                "conf/prod.py": "from .base import *\nDEBUG = envbool('DEBUG', 'True')\n",
            },
        )
        resolved = view(ctx, "conf.prod").get("DEBUG")
        assert resolved.value.literal is True
        assert resolved.value.env_dependent

    def test_a_setting_derived_from_an_earlier_one(self, tmp_path: Path) -> None:
        ctx = project(
            tmp_path,
            {
                "conf/__init__.py": "",
                "conf/settings.py": (
                    "SECRET_KEY = 'x'\nINSTALLED_APPS = []\n"
                    "SITE = 'example.test'\nALLOWED_HOSTS = [SITE, 'www.' + SITE]\n"
                ),
            },
        )
        assert view(ctx, "conf.settings").get("ALLOWED_HOSTS").value.literal == [
            "example.test",
            "www.example.test",
        ]

    def test_an_unresolvable_value_is_marked_unresolved(self, tmp_path: Path) -> None:
        ctx = project(
            tmp_path,
            {
                "conf/__init__.py": "",
                "conf/settings.py": (
                    "SECRET_KEY = 'x'\nINSTALLED_APPS = []\nALLOWED_HOSTS = mystery()\n"
                ),
            },
        )
        resolved = view(ctx, "conf.settings").get("ALLOWED_HOSTS")
        assert resolved.origin is Origin.UNRESOLVED
        assert resolved.value.is_unknown

    def test_a_resolvable_guard_takes_only_the_branch_that_runs(self, tmp_path: Path) -> None:
        # os.getenv("CI") is None when unset, so the branch does not execute.
        # Applying it anyway would report DEBUG as on for every project using
        # this entirely correct idiom.
        ctx = project(
            tmp_path,
            {
                "conf/__init__.py": "",
                "conf/settings.py": (
                    "import os\nSECRET_KEY = 'x'\nINSTALLED_APPS = []\nDEBUG = False\n"
                    "if os.getenv('CI'):\n    DEBUG = True\n"
                ),
            },
        )
        assert view(ctx, "conf.settings").get("DEBUG").value.literal is False

    def test_an_unresolvable_guard_marks_the_assignment_conditional(self, tmp_path: Path) -> None:
        ctx = project(
            tmp_path,
            {
                "conf/__init__.py": "",
                "conf/settings.py": (
                    "SECRET_KEY = 'x'\nINSTALLED_APPS = []\nDEBUG = False\n"
                    "if mystery():\n    DEBUG = True\n"
                ),
            },
        )
        resolved = view(ctx, "conf.settings").get("DEBUG")
        assert resolved.conditional
        assert resolved.value.literal is True

    def test_a_taken_branch_guarded_by_the_environment_stays_conditional(
        self, tmp_path: Path
    ) -> None:
        ctx = project(
            tmp_path,
            {
                "conf/__init__.py": "",
                "conf/settings.py": (
                    "import os\nSECRET_KEY = 'x'\nINSTALLED_APPS = []\n"
                    "if os.getenv('MODE', 'dev') == 'dev':\n    DEBUG = True\n"
                ),
            },
        )
        resolved = view(ctx, "conf.settings").get("DEBUG")
        assert resolved.value.literal is True
        assert resolved.conditional

    def test_an_else_branch_is_taken_when_the_test_is_false(self, tmp_path: Path) -> None:
        ctx = project(
            tmp_path,
            {
                "conf/__init__.py": "",
                "conf/settings.py": (
                    "SECRET_KEY = 'x'\nINSTALLED_APPS = []\n"
                    "if False:\n    DEBUG = True\nelse:\n    DEBUG = False\n"
                ),
            },
        )
        assert view(ctx, "conf.settings").get("DEBUG").value.literal is False

    def test_an_unassigned_setting_is_absent(self, overridden_project: Path) -> None:
        # A name Django holds no default for. SECURE_SSL_REDIRECT would now
        # come back as DJANGO_DEFAULT instead, which is the point of 1.3.3.
        ctx = build_context(overridden_project)
        resolved = view(ctx, "config.settings.base").get("STRIPE_SECRET_KEY")
        assert resolved.origin is Origin.ABSENT
        assert resolved.definition is None

    def test_lowercase_names_are_not_treated_as_settings(self, tmp_path: Path) -> None:
        # They are still evaluated, because settings are built from them.
        ctx = project(
            tmp_path,
            {
                "conf/__init__.py": "",
                "conf/settings.py": (
                    "SECRET_KEY = 'x'\nINSTALLED_APPS = []\n"
                    "base = 'example.test'\nALLOWED_HOSTS = [base]\n"
                ),
            },
        )
        resolved = view(ctx, "conf.settings")
        assert "base" not in resolved
        assert resolved.get("ALLOWED_HOSTS").value.literal == ["example.test"]


class TestResolveOneModule:
    def test_resolve_settings_takes_a_module_directly(self, overridden_project: Path) -> None:
        ctx = build_context(overridden_project)
        module = next(m for m in ctx.settings_modules if m.dotted.endswith("production"))
        assert resolve_settings(ctx, module).get("DEBUG").value.literal is False

    def test_an_unparseable_module_yields_an_empty_view(self, tmp_path: Path) -> None:
        broken = tmp_path / "broken.py"
        broken.write_text("SECRET_KEY = (\n")
        ctx = build_context(tmp_path)
        module = SettingsModule(path=broken, dotted="broken", role=SettingsRole.UNKNOWN)
        assert resolve_settings(ctx, module).settings == {}


def settings_source(body: str) -> dict[str, str]:
    return {
        "conf/__init__.py": "",
        "conf/settings.py": f"SECRET_KEY = 'x'\nROOT_URLCONF = 'urls'\n{body}",
    }


class TestListMutation:
    """INSTALLED_APPS and MIDDLEWARE are assembled, not declared."""

    def test_augmented_assignment(self, tmp_path: Path) -> None:
        ctx = project(
            tmp_path, settings_source("INSTALLED_APPS = ['a']\nINSTALLED_APPS += ['b']\n")
        )
        assert view(ctx, "conf.settings").get("INSTALLED_APPS").value.literal == ["a", "b"]

    def test_append(self, tmp_path: Path) -> None:
        ctx = project(
            tmp_path,
            settings_source("MIDDLEWARE = ['a']\nMIDDLEWARE.append('b')\nINSTALLED_APPS = []\n"),
        )
        assert view(ctx, "conf.settings").get("MIDDLEWARE").value.literal == ["a", "b"]

    def test_insert_respects_position(self, tmp_path: Path) -> None:
        ctx = project(
            tmp_path,
            settings_source("MIDDLEWARE = ['b']\nMIDDLEWARE.insert(0, 'a')\nINSTALLED_APPS = []\n"),
        )
        assert view(ctx, "conf.settings").get("MIDDLEWARE").value.literal == ["a", "b"]

    def test_extend(self, tmp_path: Path) -> None:
        ctx = project(
            tmp_path,
            settings_source("INSTALLED_APPS = ['a']\nINSTALLED_APPS.extend(['b', 'c'])\n"),
        )
        assert view(ctx, "conf.settings").get("INSTALLED_APPS").value.literal == ["a", "b", "c"]

    def test_remove(self, tmp_path: Path) -> None:
        ctx = project(
            tmp_path,
            settings_source("INSTALLED_APPS = ['a', 'debug_toolbar']\n"),
        )
        assert "debug_toolbar" in view(ctx, "conf.settings").get("INSTALLED_APPS").value.literal

    def test_remove_applies(self, tmp_path: Path) -> None:
        ctx = project(
            tmp_path,
            settings_source(
                "INSTALLED_APPS = ['a', 'debug_toolbar']\nINSTALLED_APPS.remove('debug_toolbar')\n"
            ),
        )
        assert view(ctx, "conf.settings").get("INSTALLED_APPS").value.literal == ["a"]

    def test_starred_reassembly(self, tmp_path: Path) -> None:
        # NetBox's `MIDDLEWARE = ["x", *MIDDLEWARE]` pattern.
        ctx = project(
            tmp_path,
            settings_source(
                "MIDDLEWARE = ['b']\nMIDDLEWARE = ['a', *MIDDLEWARE]\nINSTALLED_APPS = []\n"
            ),
        )
        assert view(ctx, "conf.settings").get("MIDDLEWARE").value.literal == ["a", "b"]

    def test_a_guarded_mutation_that_cannot_run_is_not_applied(self, tmp_path: Path) -> None:
        # The precise NetBox shape: debug tooling behind `if DEBUG:`.
        ctx = project(
            tmp_path,
            settings_source(
                "DEBUG = False\nINSTALLED_APPS = ['a']\n"
                "if DEBUG:\n    INSTALLED_APPS += ['debug_toolbar']\n"
            ),
        )
        assert view(ctx, "conf.settings").get("INSTALLED_APPS").value.literal == ["a"]

    def test_a_guarded_mutation_that_does_run_is_applied(self, tmp_path: Path) -> None:
        ctx = project(
            tmp_path,
            settings_source(
                "DEBUG = True\nINSTALLED_APPS = ['a']\n"
                "if DEBUG:\n    INSTALLED_APPS += ['debug_toolbar']\n"
            ),
        )
        assert "debug_toolbar" in view(ctx, "conf.settings").get("INSTALLED_APPS").value.literal

    def test_an_unresolvable_guard_applies_the_mutation_conditionally(self, tmp_path: Path) -> None:
        ctx = project(
            tmp_path,
            settings_source(
                "INSTALLED_APPS = ['a']\nif mystery():\n    INSTALLED_APPS += ['debug_toolbar']\n"
            ),
        )
        resolved = view(ctx, "conf.settings").get("INSTALLED_APPS")
        assert "debug_toolbar" in resolved.value.literal
        assert resolved.conditional

    def test_mutation_across_the_inheritance_chain(self, tmp_path: Path) -> None:
        ctx = project(
            tmp_path,
            {
                "conf/__init__.py": "",
                "conf/base.py": "SECRET_KEY = 'x'\nINSTALLED_APPS = ['a']\nROOT_URLCONF = 'u'\n",
                "conf/prod.py": "from .base import *\nINSTALLED_APPS += ['b']\n",
            },
        )
        assert view(ctx, "conf.prod").get("INSTALLED_APPS").value.literal == ["a", "b"]

    def test_mutation_of_an_unresolved_list_is_unknown(self, tmp_path: Path) -> None:
        ctx = project(
            tmp_path,
            settings_source("INSTALLED_APPS = mystery()\nINSTALLED_APPS.append('a')\n"),
        )
        assert view(ctx, "conf.settings").get("INSTALLED_APPS").value.is_unknown

    def test_mutation_before_any_assignment_is_ignored(self, tmp_path: Path) -> None:
        ctx = project(
            tmp_path,
            settings_source("MIDDLEWARE.append('a')\nINSTALLED_APPS = []\n"),
        )
        assert "MIDDLEWARE" not in view(ctx, "conf.settings")

    def test_removing_a_missing_entry_does_not_crash(self, tmp_path: Path) -> None:
        ctx = project(
            tmp_path,
            settings_source("INSTALLED_APPS = ['a']\nINSTALLED_APPS.remove('nope')\n"),
        )
        assert view(ctx, "conf.settings").get("INSTALLED_APPS").value.is_unknown

    def test_the_mutation_appears_in_the_provenance_chain(self, tmp_path: Path) -> None:
        ctx = project(
            tmp_path,
            settings_source("INSTALLED_APPS = ['a']\nINSTALLED_APPS += ['b']\n"),
        )
        resolved = view(ctx, "conf.settings").get("INSTALLED_APPS")
        assert len(resolved.definitions) == 2

    def test_incompatible_augmented_assignment_is_unknown(self, tmp_path: Path) -> None:
        ctx = project(tmp_path, settings_source("INSTALLED_APPS = ['a']\nINSTALLED_APPS += 5\n"))
        assert view(ctx, "conf.settings").get("INSTALLED_APPS").value.is_unknown

    def test_taint_survives_a_mutation(self, tmp_path: Path) -> None:
        ctx = project(
            tmp_path,
            settings_source(
                "import os\nINSTALLED_APPS = [os.getenv('APP', 'a')]\nINSTALLED_APPS += ['b']\n"
            ),
        )
        assert view(ctx, "conf.settings").get("INSTALLED_APPS").value.env_dependent

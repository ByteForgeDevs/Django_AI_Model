"""``django-configurations``: settings in a class body.

The regression guarded here is total rather than partial. Before this support
existed, a project using this library produced *no* settings module, so every
``DJS`` rule was skipped and the run exited having audited nothing -- the
failure mode this tool exists to avoid. The tests are therefore weighted
towards what must stay *silent*, because the way this feature fails in practice
is not blindness but noise: reporting a development class as though it were
production.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from djaudit import engine
from djaudit.configurations import (
    applied_classes,
    build_index,
    class_name_tokens,
    find_configuration_classes,
)
from djaudit.context import SettingsRole
from djaudit.discovery import build_context, classify_configuration_role
from djaudit.evaluator import Scope, evaluate
from djaudit.models import Confidence, Severity
from djaudit.settings import resolve_settings

VALUES_SCOPE = Scope(imports={"values": "configurations.values"})


def value_of(source: str):
    return evaluate(ast.parse(source, mode="eval").body, VALUES_SCOPE)


def build(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "proj"
    for name, text in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    return root


SIMPLE = {
    "manage.py": "import os\n",
    "config/__init__.py": "",
    "config/settings.py": (
        "from configurations import Configuration, values\n\n\n"
        "class Base(Configuration):\n"
        "    SECRET_KEY = values.SecretValue()\n"
        "    DEBUG = False\n"
        "    ALLOWED_HOSTS = ['example.com']\n"
        "    INSTALLED_APPS = ['django.contrib.auth']\n\n\n"
        "class Dev(Base):\n"
        "    DEBUG = True\n\n\n"
        "class Prod(Base):\n"
        "    SESSION_COOKIE_SECURE = False\n"
    ),
}


class TestValues:
    """``values.X(default)`` is an environment read with a shipped default."""

    def test_a_default_is_the_value_the_environment_falls_back_to(self):
        resolved = value_of("values.BooleanValue(False)")
        assert resolved.is_literal
        assert resolved.literal is False

    def test_a_default_is_marked_environment_dependent(self):
        """`DJANGO_DEBUG` can override it, so confidence has to reflect that."""
        assert value_of("values.BooleanValue(False)").env_dependent

    def test_a_secret_value_never_resolves_to_a_literal(self):
        """`SecretValue.__init__` raises if given a default, so there is none.

        Resolving one to a literal would report the library's own recommended
        way of holding a secret as a hardcoded secret.
        """
        resolved = value_of("values.SecretValue()")
        assert not resolved.is_literal
        assert resolved.env_dependent

    def test_environ_false_means_the_default_is_simply_the_value(self):
        resolved = value_of("values.BooleanValue(True, environ=False)")
        assert resolved.literal is True
        assert not resolved.env_dependent

    def test_environ_required_beats_a_default_that_is_written_anyway(self):
        """`setup()` raises rather than falling back, so the default is dead."""
        assert not value_of("values.Value('d', environ_required=True)").is_literal

    def test_environ_required_is_inert_when_the_environment_is_not_read(self):
        resolved = value_of("values.Value('d', environ_required=True, environ=False)")
        assert resolved.literal == "d"

    def test_the_default_may_be_passed_by_keyword(self):
        assert value_of("values.BooleanValue(default=False)").literal is False

    def test_a_bare_value_has_no_default_to_find(self):
        assert value_of("values.Value()").literal is None

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ("values.IntegerValue(5)", 5),
            ("values.ListValue(['a'])", ["a"]),
            ("values.Value('x')", "x"),
        ],
    )
    def test_every_value_class_takes_its_default_first(self, source, expected):
        assert value_of(source).literal == expected

    def test_an_unrelated_call_is_not_mistaken_for_one(self):
        """A control: the match is on the import, not on the spelling."""
        scope = Scope(imports={"values": "myapp.values"})
        resolved = evaluate(ast.parse("values.BooleanValue(False)", mode="eval").body, scope)
        assert not resolved.is_literal


class TestFindingTheClasses:
    def test_a_direct_subclass_is_found(self):
        tree = ast.parse("from configurations import Configuration\nclass A(Configuration): pass\n")
        assert list(find_configuration_classes(tree).classes) == ["A"]

    def test_inheritance_is_followed_without_naming_configuration_again(self):
        tree = ast.parse(
            "from configurations import Configuration\n"
            "class A(Configuration): pass\n"
            "class B(A): pass\n"
        )
        assert list(find_configuration_classes(tree).classes) == ["A", "B"]

    def test_an_unrelated_class_is_not_collected(self):
        tree = ast.parse("from configurations import Configuration\nclass A: pass\n")
        assert not find_configuration_classes(tree).classes

    def test_a_base_that_something_derives_from_is_marked_inherited(self):
        tree = ast.parse(
            "from configurations import Configuration\n"
            "class A(Configuration): pass\n"
            "class B(A): pass\n"
        )
        found = find_configuration_classes(tree)
        assert found.leaves == ["B"]

    def test_the_chain_runs_base_first(self):
        tree = ast.parse(
            "from configurations import Configuration\n"
            "class A(Configuration): pass\n"
            "class B(A): pass\n"
        )
        assert [c.name for c in find_configuration_classes(tree).chain("B")] == ["A", "B"]

    def test_the_leftmost_base_is_applied_last_of_the_bases(self):
        """`ConfigurationBase.__new__` folds bases in reverse, so left wins."""
        tree = ast.parse(
            "from configurations import Configuration\n"
            "class L(Configuration): pass\n"
            "class R(Configuration): pass\n"
            "class C(L, R): pass\n"
        )
        assert [c.name for c in find_configuration_classes(tree).chain("C")] == ["R", "L", "C"]


class TestCrossFileInheritance:
    """The documented split layout puts the base class in its own module."""

    def trees(self, sources: dict[str, str]) -> dict[Path, ast.Module]:
        return {Path(name): ast.parse(text) for name, text in sources.items()}

    def test_a_base_imported_from_another_file_is_resolved(self):
        trees = self.trees(
            {
                "base.py": (
                    "from configurations import Configuration\nclass Base(Configuration): pass\n"
                ),
                "prod.py": "from .base import Base\nclass Prod(Base): pass\n",
            }
        )
        index = build_index(trees, lambda importer, name: Path("base.py"))
        assert list(index[Path("prod.py")].classes) == ["Prod"]

    def test_a_chain_several_files_deep_settles(self):
        trees = self.trees(
            {
                "a.py": "from configurations import Configuration\nclass A(Configuration): pass\n",
                "b.py": "from .a import A\nclass B(A): pass\n",
                "c.py": "from .b import B\nclass C(B): pass\n",
            }
        )
        resolved = {"a": Path("a.py"), "b": Path("b.py")}
        index = build_index(trees, lambda importer, name: resolved.get(name.lstrip(".")))
        assert list(index[Path("c.py")].classes) == ["C"]

    def test_an_import_that_leads_nowhere_is_not_a_configuration(self):
        """A control: the fixpoint must not mark everything it cannot resolve."""
        trees = self.trees({"prod.py": "from .base import Base\nclass Prod(Base): pass\n"})
        index = build_index(trees, lambda importer, name: None)
        assert not index


class TestHandRolledLoaders:
    """readthedocs.org, the project this step exists for, does not use the library.

    It defines ``class Settings`` with a ``load_settings(cls, module_name)``
    classmethod that copies every uppercase attribute onto the named module --
    the same semantics under a different name. Recognition is therefore
    structural: a module-level ``X.method(__name__)`` call is direct evidence
    that class *becomes* this settings module.
    """

    def trees(self, sources: dict[str, str]) -> dict[Path, ast.Module]:
        return {Path(name): ast.parse(text) for name, text in sources.items()}

    def test_a_class_applied_to_this_module_is_found(self):
        tree = ast.parse("class Dev(Base): pass\nDev.load_settings(__name__)\n")
        assert applied_classes(tree) == {"Dev"}

    def test_the_method_name_does_not_matter(self):
        """Structural, not a hardcoded list: any project may name its own loader."""
        tree = ast.parse("Dev.install(__name__)\n")
        assert applied_classes(tree) == {"Dev"}

    def test_a_call_on_some_other_argument_is_not_evidence(self):
        """A control. Without ``__name__`` the call says nothing about this module."""
        tree = ast.parse("Dev.load_settings('other.module')\n")
        assert applied_classes(tree) == set()

    def test_a_bare_call_is_not_evidence(self):
        """A control: ``configure(__name__)`` names no class."""
        assert applied_classes(ast.parse("configure(__name__)\n")) == set()

    def test_a_nested_call_is_not_evidence(self):
        """A control: only module level counts, or every helper would qualify."""
        tree = ast.parse("def go():\n    Dev.load_settings(__name__)\n")
        assert applied_classes(tree) == set()

    def test_the_applied_class_becomes_a_configuration(self):
        trees = self.trees({"prod.py": "class Prod: DEBUG = True\nProd.load_settings(__name__)\n"})
        index = build_index(trees, lambda importer, name: None)
        assert list(index[Path("prod.py")].classes) == ["Prod"]

    def test_the_bases_of_an_applied_class_are_chained(self):
        """The fixpoint must run upwards too, or inherited defaults are lost.

        readthedocs' hardcoded ``SECRET_KEY`` lives three files above the class
        that is actually applied.
        """
        trees = self.trees(
            {
                "base.py": "class Base: SECRET_KEY = 'x'\n",
                "prod.py": (
                    "from .base import Base\n"
                    "class Prod(Base): DEBUG = True\n"
                    "Prod.load_settings(__name__)\n"
                ),
            }
        )
        index = build_index(trees, lambda importer, name: Path("base.py"))
        assert list(index[Path("base.py")].classes) == ["Base"]

    def test_an_ordinary_class_is_not_a_configuration(self):
        """The control for all of the above: no apply call, no settings module."""
        trees = self.trees({"prod.py": "class Prod: DEBUG = True\n"})
        assert build_index(trees, lambda importer, name: None) == {}


class TestEmptyBasesAreNotModules:
    """An empty loader base holds nothing, so it cannot be missing anything.

    Measured on readthedocs.org: its ``class Settings`` defines only a
    classmethod, and auditing it as a settings module of its own produced two
    false positives for security settings it was never going to declare.
    """

    def trees(self, sources: dict[str, str]) -> dict[Path, ast.Module]:
        return {Path(name): ast.parse(text) for name, text in sources.items()}

    MARKERS = frozenset({"DEBUG", "SECRET_KEY", "INSTALLED_APPS"})

    def index(self, sources: dict[str, str], resolve=lambda i, n: Path("base.py")):
        return build_index(self.trees(sources), resolve, self.MARKERS)

    def test_a_class_holding_only_a_method_defines_nothing(self):
        index = self.index(
            {
                "core.py": "class Settings:\n    @classmethod\n    def load(cls, m): pass\n",
                "prod.py": (
                    "from .core import Settings\n"
                    "class Prod(Settings): DEBUG = True\n"
                    "Prod.load(__name__)\n"
                ),
            },
            lambda importer, name: Path("core.py"),
        )
        assert index[Path("core.py")].classes["Settings"].defines_settings is False

    def test_a_class_that_assigns_a_setting_defines_settings(self):
        """The presence control: without it the assertion above proves nothing."""
        index = self.index({"base.py": "class Base: DEBUG = True\nBase.load(__name__)\n"})
        assert index[Path("base.py")].classes["Base"].defines_settings is True

    def test_an_empty_subclass_inherits_the_fact(self):
        """``class Prod(Base): pass`` is a real override point and must survive."""
        index = self.index(
            {"base.py": "class Base: SECRET_KEY = 'x'\n"}
            | {"prod.py": "from .base import Base\nclass Prod(Base): pass\nProd.load(__name__)\n"}
        )
        assert index[Path("prod.py")].classes["Prod"].defines_settings is True

    def test_a_non_setting_assignment_does_not_count(self):
        """A control: any uppercase name would otherwise make every class qualify."""
        source = "class Base: HELPER_PATH = '/x'\nBase.load(__name__)\n"
        index = self.index({"base.py": source})
        assert index[Path("base.py")].classes["Base"].defines_settings is False

    def test_an_empty_base_is_not_reported_as_a_settings_module(self, tmp_path: Path):
        """End to end: the two false positives measured on readthedocs.org."""
        root = build(
            tmp_path,
            {
                "manage.py": "import os\nos.environ['DJANGO_SETTINGS_MODULE'] = 'conf.prod'\n",
                "conf/__init__.py": "",
                "conf/core.py": "class Settings:\n    @classmethod\n    def load(cls, m): pass\n",
                "conf/prod.py": (
                    "from .core import Settings\n"
                    "class Prod(Settings):\n"
                    "    DEBUG = False\n"
                    "    SECRET_KEY = 'hunter2'\n"
                    "    ALLOWED_HOSTS = ['example.com']\n"
                    "Prod.load(__name__)\n"
                ),
            },
        )
        ctx = build_context(root)
        assert [m.dotted for m in ctx.settings_modules] == ["conf.prod.Prod"]


class TestRoles:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("Dev", SettingsRole.DEVELOPMENT),
            ("Local", SettingsRole.DEVELOPMENT),
            ("Prod", SettingsRole.PRODUCTION),
            ("Staging", SettingsRole.PRODUCTION),
            ("ProductionConfig", SettingsRole.PRODUCTION),
            ("Base", SettingsRole.BASE),
            ("Test", SettingsRole.TEST),
        ],
    )
    def test_the_class_name_decides_the_role(self, name, expected):
        assert classify_configuration_role(name, Path("config/settings.py")) is expected

    def test_camel_case_is_split_so_the_keyword_is_visible(self):
        """`productionconfig` matches nothing; `production` matches."""
        assert "production" in class_name_tokens("ProductionConfig")

    def test_a_silent_name_falls_back_to_the_file(self):
        role = classify_configuration_role("Settings", Path("config/settings.py"))
        assert role is SettingsRole.PRIMARY

    def test_a_silent_name_in_a_development_file_follows_the_file(self):
        role = classify_configuration_role("Settings", Path("config/development.py"))
        assert role is SettingsRole.DEVELOPMENT


class TestDiscovery:
    def test_every_class_becomes_its_own_settings_module(self, tmp_path):
        ctx = build_context(build(tmp_path, SIMPLE))
        assert [m.dotted for m in ctx.settings_modules] == [
            "config.settings.Base",
            "config.settings.Dev",
            "config.settings.Prod",
        ]

    def test_each_module_carries_the_class_it_stands_for(self, tmp_path):
        ctx = build_context(build(tmp_path, SIMPLE))
        assert {m.configuration_class for m in ctx.settings_modules} == {"Base", "Dev", "Prod"}

    def test_the_roles_come_from_the_class_names(self, tmp_path):
        ctx = build_context(build(tmp_path, SIMPLE))
        roles = {m.configuration_class: m.role for m in ctx.settings_modules}
        assert roles["Dev"] is SettingsRole.DEVELOPMENT
        assert roles["Prod"] is SettingsRole.PRODUCTION

    def test_no_diagnostic_is_raised_for_a_shape_that_now_resolves(self, tmp_path):
        assert build_context(build(tmp_path, SIMPLE)).diagnostics == ()


class TestResolution:
    def resolve(self, tmp_path, name: str, files: dict[str, str] | None = None):
        ctx = build_context(build(tmp_path, files or SIMPLE))
        module = next(m for m in ctx.settings_modules if m.configuration_class == name)
        return resolve_settings(ctx, module)

    def test_a_class_body_defines_settings_at_all(self, tmp_path):
        view = self.resolve(tmp_path, "Base")
        assert view.settings["ALLOWED_HOSTS"].value.literal == ["example.com"]

    def test_a_subclass_inherits_what_it_does_not_override(self, tmp_path):
        view = self.resolve(tmp_path, "Prod")
        assert view.settings["ALLOWED_HOSTS"].value.literal == ["example.com"]

    def test_a_subclass_overrides_its_base(self, tmp_path):
        assert self.resolve(tmp_path, "Dev").settings["DEBUG"].value.literal is True

    def test_the_base_keeps_its_own_value(self, tmp_path):
        """The control for the test above: overriding must not leak upwards."""
        assert self.resolve(tmp_path, "Base").settings["DEBUG"].value.literal is False

    def test_a_sibling_does_not_see_another_subclass(self, tmp_path):
        assert self.resolve(tmp_path, "Prod").settings["DEBUG"].value.literal is False

    def test_a_setting_only_a_subclass_defines_is_found(self, tmp_path):
        view = self.resolve(tmp_path, "Prod")
        assert view.settings["SESSION_COOKIE_SECURE"].value.literal is False

    def test_lowercase_class_attributes_are_not_settings(self, tmp_path):
        files = dict(SIMPLE)
        files["config/settings.py"] = (
            "from configurations import Configuration\n\n\n"
            "class Base(Configuration):\n"
            "    DEBUG = False\n"
            "    INSTALLED_APPS = []\n"
            "    helper = 1\n"
        )
        assert "helper" not in self.resolve(tmp_path, "Base", files).settings


class TestTheWholeRun:
    """The end-to-end contrast: this shape used to produce nothing at all."""

    def findings(self, root: Path):
        result = engine.run(root, min_severity=Severity.INFO, min_confidence=Confidence.TENTATIVE)
        return {(f.rule_id, f.location.line) for f in result.findings}

    def test_the_fixture_reports_its_planted_defects(self, configurations_project):
        found = self.findings(configurations_project)
        assert ("DJS-003", 124) in found
        assert ("DJS-009", 127) in found

    def test_the_development_class_is_silent(self, configurations_project):
        """Five lines in `Dev` are word-for-word findings inside `Prod`."""
        found = self.findings(configurations_project)
        assert not [key for key in found if 100 <= key[1] <= 118]

    def test_the_run_is_not_blocked_by_a_diagnostic(self, configurations_project):
        result = engine.run(configurations_project)
        assert [d.code for d in result.context.diagnostics] == []

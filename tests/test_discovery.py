"""Discovery decides which files are audited at all, so mistakes here are silent."""

from pathlib import Path

from djaudit.context import SettingsRole
from djaudit.discovery import (
    build_context,
    classify_settings_role,
    detect_django_version,
    dotted_path,
    iter_python_files,
)


class TestRoleClassification:
    def test_environment_names(self):
        cases = {
            "production.py": SettingsRole.PRODUCTION,
            "prod.py": SettingsRole.PRODUCTION,
            "staging.py": SettingsRole.PRODUCTION,
            "development.py": SettingsRole.DEVELOPMENT,
            "local.py": SettingsRole.DEVELOPMENT,
            "test.py": SettingsRole.TEST,
            "base.py": SettingsRole.BASE,
            "common.py": SettingsRole.BASE,
            "settings.py": SettingsRole.PRIMARY,
        }
        for name, expected in cases.items():
            assert classify_settings_role(Path(name)) is expected, name

    def test_compound_names_prefer_the_environment_token(self):
        assert classify_settings_role(Path("test_settings.py")) is SettingsRole.TEST
        assert classify_settings_role(Path("settings_production.py")) is SettingsRole.PRODUCTION

    def test_package_init_takes_the_directory_name(self):
        path = Path("conf/production/__init__.py")
        assert classify_settings_role(path) is SettingsRole.PRODUCTION

    def test_a_test_directory_names_the_role_when_the_file_does_not(self, tmp_path):
        """pretix keeps real test settings in ``pretix/testutils/settings.py``.

        The stem reads as an ordinary primary settings module, so before the
        directory was consulted its deliberate ``DEBUG = True`` and MD5 password
        hasher were reported as critical production findings.
        """
        root = tmp_path / "project"
        path = root / "src/pretix/testutils/settings.py"
        path.parent.mkdir(parents=True)
        path.touch()
        assert classify_settings_role(path, root) is SettingsRole.TEST
        assert classify_settings_role(path) is SettingsRole.PRIMARY

    def test_the_filename_outranks_the_directory(self, tmp_path):
        """``tests/production.py`` is still about production."""
        root = tmp_path / "project"
        path = root / "tests/production.py"
        path.parent.mkdir(parents=True)
        path.touch()
        assert classify_settings_role(path, root) is SettingsRole.PRODUCTION

    def test_directories_above_the_root_are_not_read(self, tmp_path):
        """A checkout that happens to live under ``/tmp/test/`` is not test code.

        This is the direction that hides findings, so it is the one worth
        pinning: only the path below the project root may downgrade a module.
        """
        root = tmp_path / "test" / "project"
        path = root / "myproj/settings.py"
        path.parent.mkdir(parents=True)
        path.touch()
        assert classify_settings_role(path, root) is SettingsRole.PRIMARY

    def test_a_directory_cannot_promote_to_production(self, tmp_path):
        """Only test and development are inferred; the rest need a filename."""
        root = tmp_path / "project"
        path = root / "deploy/myproj.py"
        path.parent.mkdir(parents=True)
        path.touch()
        assert classify_settings_role(path, root) is SettingsRole.UNKNOWN

    def test_unrecognised_names_stay_production_reaching(self):
        """Guessing wrong is asymmetric: mislabelling prod as dev hides findings."""
        role = classify_settings_role(Path("weird_name.py"))
        assert role is SettingsRole.UNKNOWN
        assert role.reaches_production

    def test_only_dev_and_test_are_exempt(self):
        exempt = {r for r in SettingsRole if not r.reaches_production}
        assert exempt == {SettingsRole.DEVELOPMENT, SettingsRole.TEST}


class TestFileWalk:
    def test_caches_and_vcs_directories_are_skipped(self, tmp_path):
        (tmp_path / "app").mkdir()
        (tmp_path / "app" / "models.py").write_text("x = 1")
        for skipped in ("__pycache__", ".git", "node_modules"):
            (tmp_path / skipped).mkdir()
            (tmp_path / skipped / "junk.py").write_text("x = 1")

        found = {p.name for p in iter_python_files(tmp_path)}
        assert found == {"models.py"}

    def test_virtualenvs_are_detected_by_pyvenv_cfg_not_by_name(self, tmp_path):
        """A real app package can legitimately be called 'env'."""
        env = tmp_path / "env"
        env.mkdir()
        (env / "pyvenv.cfg").write_text("home = /usr")
        (env / "sitecustomize.py").write_text("x = 1")

        app = tmp_path / "environment"
        app.mkdir()
        (app / "models.py").write_text("x = 1")

        found = {p.name for p in iter_python_files(tmp_path)}
        assert found == {"models.py"}


class TestDottedPath:
    def test_module(self, tmp_path):
        assert dotted_path(tmp_path, tmp_path / "config" / "settings.py") == "config.settings"

    def test_package_init_drops_the_init(self, tmp_path):
        path = tmp_path / "config" / "settings" / "__init__.py"
        assert dotted_path(tmp_path, path) == "config.settings"


class TestDjangoVersion:
    def test_reads_a_requirements_pin(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("Django==5.2.15\n")
        assert detect_django_version(tmp_path) == "5.2.15"

    def test_handles_extras(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("Django[argon2]==6.0.7\n")
        assert detect_django_version(tmp_path) == "6.0.7"

    def test_ignores_other_django_packages(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("django-cors-headers==4.9.0\nDjango==6.0.7\n")
        assert detect_django_version(tmp_path) == "6.0.7"

    def test_absent_pin_returns_none(self, tmp_path):
        assert detect_django_version(tmp_path) is None


class TestProjectDiscovery:
    def test_finds_the_split_settings_layout(self, vulnerable_project):
        ctx = build_context(vulnerable_project)
        by_role = {m.role: m.dotted for m in ctx.settings_modules}

        assert by_role[SettingsRole.BASE] == "config.settings.base"
        assert by_role[SettingsRole.PRODUCTION] == "config.settings.production"
        assert by_role[SettingsRole.DEVELOPMENT] == "config.settings.development"

    def test_reads_the_entrypoint_from_manage_py_without_executing_it(self, vulnerable_project):
        ctx = build_context(vulnerable_project)
        assert ctx.settings_entrypoint == "config.settings.production"
        assert any(
            m.is_entrypoint and m.role is SettingsRole.PRODUCTION for m in ctx.settings_modules
        )

    def test_empty_settings_package_init_is_not_treated_as_settings(self, vulnerable_project):
        ctx = build_context(vulnerable_project)
        assert "config.settings" not in {m.dotted for m in ctx.settings_modules}

    def test_modules_are_confirmed_by_star_import_from_a_settings_module(self, vulnerable_project):
        """production.py defines no markers of its own; it inherits via `import *`."""
        ctx = build_context(vulnerable_project)
        assert "config.settings.production" in {m.dotted for m in ctx.settings_modules}


class TestRobustness:
    def test_a_syntax_error_is_recorded_and_does_not_abort_the_run(self, tmp_path):
        (tmp_path / "broken.py").write_text("def (:\n")
        (tmp_path / "settings.py").write_text("INSTALLED_APPS = []\n")

        ctx = build_context(tmp_path)
        assert ctx.parse(tmp_path / "broken.py") is None
        assert tmp_path / "broken.py" in ctx.parse_errors
        assert {m.dotted for m in ctx.settings_modules} == {"settings"}

    def test_parsing_is_cached(self, vulnerable_project):
        ctx = build_context(vulnerable_project)
        path = vulnerable_project / "config" / "settings" / "base.py"
        assert ctx.parse(path) is ctx.parse(path)


class TestModulePath:
    """The inverse of ``dotted_path``, which the cookie rules use to read a
    middleware class named only as a string in ``MIDDLEWARE``."""

    def build(self, tmp_path: Path, *relative: str) -> Path:
        root = tmp_path / "project"
        for name in relative:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("")
        return root

    def test_finds_a_module_by_its_dotted_name(self, tmp_path):
        root = self.build(tmp_path, "myproj/mw.py")
        ctx = build_context(root)
        assert ctx.module_path("myproj.mw") == root / "myproj/mw.py"

    def test_a_src_layout_resolves_without_its_import_root(self, tmp_path):
        """pretix's shape: modules live under ``src/`` and never say so."""
        root = self.build(tmp_path, "src/pretix/multidomain/middlewares.py")
        ctx = build_context(root)
        found = ctx.module_path("pretix.multidomain.middlewares")
        assert found == root / "src/pretix/multidomain/middlewares.py"

    def test_an_ambiguous_suffix_resolves_to_nothing(self, tmp_path):
        """Two apps ending in ``.models`` must not resolve to whichever was walked first."""
        root = self.build(tmp_path, "a/app/models.py", "b/app/models.py")
        ctx = build_context(root)
        assert ctx.module_path("app.models") is None

    def test_a_module_that_is_not_ours_is_not_invented(self, tmp_path):
        ctx = build_context(self.build(tmp_path, "myproj/mw.py"))
        assert ctx.module_path("django.contrib.sessions.middleware") is None

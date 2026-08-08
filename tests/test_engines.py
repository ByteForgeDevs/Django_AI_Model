"""Which database vendor a settings module can reach -- substep 5.1.1.

Two properties are worth more than the rest here.

The first is that :func:`classify` is a table and not a substring test, so the
tests that matter are the ones a substring test would fail: `spatialite`
contains no `sqlite`, `postgis` contains no `postgres`, and a wrapper such as
`django_prometheus.db.backends.postgresql` puts the vendor nowhere near the
front of the string. Each of those has an assertion of its own rather than
being folded into a table-driven loop, because a loop over the same dict the
implementation uses cannot see a wrong entry.

The second is that "no engine found" and "SQLite" must never collapse. NetBox
builds `DATABASES` with `getattr` on an imported module and sets `ENGINE`
through `.update()`, so we genuinely cannot read it; a rule that treated that
silence as "not SQLite, therefore fine" would go quiet on exactly the projects
that are hardest to analyse.
"""

from __future__ import annotations

import dataclasses
import pathlib

import pytest

from djaudit.context import SettingsModule, SettingsRole
from djaudit.discovery import build_context
from djaudit.engines import (
    Divergence,
    EngineChoice,
    Portability,
    Vendor,
    classify,
    divergence,
    module_engines,
    module_name,
    project_engines,
)
from djaudit.settings import resolve_all

MARKERS = "INSTALLED_APPS = []\nSECRET_KEY = 'x'\nDEBUG = False\n"

LITE = "django.db.backends.sqlite3"
PG = "django.db.backends.postgresql"
MY = "django.db.backends.mysql"


def build(tmp_path: pathlib.Path, body: str, **extra: str) -> pathlib.Path:
    """A project whose settings module contains `body`.

    Each call gets its own root so a test can build two projects and compare
    them without the second silently overwriting the first.
    """
    root = tmp_path / f"proj{len(list(tmp_path.iterdir()))}"
    (root / "config").mkdir(parents=True)
    (root / "manage.py").write_text(
        "import os\nos.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')\n"
    )
    (root / "config" / "__init__.py").write_text("")
    (root / "config" / "settings.py").write_text(MARKERS + body)
    for name, text in extra.items():
        (root / "config" / f"{name}.py").write_text(MARKERS + text)
    return root


def choices(root: pathlib.Path) -> tuple[EngineChoice, ...]:
    ctx = build_context(root)
    return project_engines(ctx, resolve_all(ctx))


def one_db(engine: str) -> str:
    return f"DATABASES = {{'default': {{'ENGINE': '{engine}', 'NAME': 'app'}}}}\n"


class TestClassifyingAnEngineString:
    """The vendor a backend path names."""

    def test_the_plain_django_backends_are_read(self) -> None:
        assert classify(LITE) is Vendor.SQLITE
        assert classify(PG) is Vendor.POSTGRESQL
        assert classify(MY) is Vendor.MYSQL
        assert classify("django.db.backends.oracle") is Vendor.ORACLE

    def test_spatialite_is_sqlite_though_it_does_not_contain_sqlite(self) -> None:
        # The reason this module has a table instead of `"sqlite" in engine`.
        engine = "django.contrib.gis.db.backends.spatialite"
        assert "sqlite" not in engine
        assert classify(engine) is Vendor.SQLITE

    def test_postgis_is_postgresql_though_it_does_not_contain_postgres(self) -> None:
        engine = "django.contrib.gis.db.backends.postgis"
        assert "postgres" not in engine
        assert classify(engine) is Vendor.POSTGRESQL

    def test_a_wrapper_backend_is_read_through_to_its_vendor(self) -> None:
        # NetBox ships this one when metrics are enabled.
        assert classify("django_prometheus.db.backends.postgresql") is Vendor.POSTGRESQL
        assert classify("django_prometheus.db.backends.sqlite3") is Vendor.SQLITE

    def test_the_psycopg2_spelling_is_still_postgres(self) -> None:
        assert classify("django.db.backends.postgresql_psycopg2") is Vendor.POSTGRESQL

    def test_a_backend_suffix_is_stripped(self) -> None:
        assert classify("django_tenants.postgresql_backend") is Vendor.POSTGRESQL

    def test_a_backend_named_only_by_its_package_is_matched_whole(self) -> None:
        assert classify("psqlextra.backend") is Vendor.POSTGRESQL
        assert classify("mysql.connector.django") is Vendor.MYSQL

    def test_an_unrecognised_backend_is_unknown_not_a_guess(self) -> None:
        assert classify("acme.db.backends.thing") is Vendor.UNKNOWN
        assert classify("") is Vendor.UNKNOWN
        assert classify(None) is Vendor.UNKNOWN

    def test_unknown_is_the_only_vendor_that_is_not_known(self) -> None:
        assert not Vendor.UNKNOWN.known
        assert all(v.known for v in Vendor if v is not Vendor.UNKNOWN)

    def test_the_vendor_names_are_the_ones_findings_will_print(self) -> None:
        # `Vendor` is a `StrEnum` precisely so a message can interpolate it, so
        # the values are output and not an implementation detail.
        assert [str(v) for v in Vendor] == [
            "sqlite",
            "postgresql",
            "mysql",
            "oracle",
            "unknown",
        ]

    def test_a_backend_suffix_alone_is_not_stripped(self) -> None:
        # Only `_backend` is stripped. A bare `backend` leaf carries no vendor,
        # so packages that use one are matched on the whole path instead.
        assert classify("acme.backend") is Vendor.UNKNOWN

    def test_surrounding_whitespace_does_not_hide_the_vendor(self) -> None:
        assert classify(f"  {PG}  ") is Vendor.POSTGRESQL

    def test_the_django_backends_prefix_alone_names_no_vendor(self) -> None:
        # pretix concatenates a config value onto exactly this string, so the
        # prefix on its own must not resolve to anything.
        assert classify("django.db.backends.") is Vendor.UNKNOWN


class TestReadingOneSettingsModule:
    """What `module_engines` finds in a single file."""

    def test_a_single_engine_is_the_default(self, tmp_path: pathlib.Path) -> None:
        found = choices(build(tmp_path, one_db(PG)))
        assert len(found) == 1
        assert found[0].vendor is Vendor.POSTGRESQL
        assert found[0].alias == "default"
        assert found[0].default is True
        assert found[0].engine == PG

    def test_a_conditional_assignment_is_not_the_default(self, tmp_path: pathlib.Path) -> None:
        # The Healthchecks shape: SQLite plainly, Postgres behind an env var.
        body = one_db(LITE) + "import os\nif os.getenv('DB') == 'postgres':\n    " + one_db(PG)
        found = choices(build(tmp_path, body))
        assert [(c.vendor, c.default) for c in found] == [
            (Vendor.SQLITE, True),
            (Vendor.POSTGRESQL, False),
        ]

    def test_three_assignments_are_all_reported(self, tmp_path: pathlib.Path) -> None:
        body = (
            one_db(LITE)
            + "import os\nif os.getenv('DB') == 'postgres':\n    "
            + one_db(PG)
            + "if os.getenv('DB') == 'mysql':\n    "
            + one_db(MY)
        )
        assert [c.vendor for c in choices(build(tmp_path, body))] == [
            Vendor.SQLITE,
            Vendor.POSTGRESQL,
            Vendor.MYSQL,
        ]

    def test_every_alias_is_reported_not_only_default(self, tmp_path: pathlib.Path) -> None:
        # A Postgres default with a SQLite replica is a portability problem
        # that reading only `default` would miss entirely.
        body = (
            f"DATABASES = {{'default': {{'ENGINE': '{PG}'}}, 'replica': {{'ENGINE': '{LITE}'}}}}\n"
        )
        found = {(c.alias, c.vendor) for c in choices(build(tmp_path, body))}
        assert found == {("default", Vendor.POSTGRESQL), ("replica", Vendor.SQLITE)}

    def test_an_unreadable_engine_is_unknown_and_still_reported(
        self, tmp_path: pathlib.Path
    ) -> None:
        # The pretix shape. Reporting nothing here would look identical to a
        # project with no database at all.
        body = (
            "import os\nbackend = os.environ['DB_BACKEND']\n"
            "DATABASES = {'default': {'ENGINE': 'django.db.backends.' + backend}}\n"
        )
        found = choices(build(tmp_path, body))
        assert len(found) == 1
        assert found[0].vendor is Vendor.UNKNOWN
        assert found[0].engine is None

    def test_a_module_without_databases_yields_nothing(self, tmp_path: pathlib.Path) -> None:
        assert choices(build(tmp_path, "")) == ()

    def test_an_annotated_assignment_is_still_an_assignment(self, tmp_path: pathlib.Path) -> None:
        # `database_configs` accepts `AnnAssign` as well as `Assign`; without
        # this the annotated spelling would read as a project with no database.
        body = f"DATABASES: dict = {{'default': {{'ENGINE': '{PG}'}}}}\n"
        assert [c.vendor for c in choices(build(tmp_path, body))] == [Vendor.POSTGRESQL]

    def test_an_annotation_without_a_value_assigns_nothing(self, tmp_path: pathlib.Path) -> None:
        # The contrast for the test above: `DATABASES: dict` declares a type
        # and connects to nothing.
        assert choices(build(tmp_path, "DATABASES: dict\n")) == ()

    def test_a_choice_cannot_be_edited_after_it_is_built(self, tmp_path: pathlib.Path) -> None:
        # Every rule in this family reads the same choices, so one rewriting a
        # vendor in place would change what the next one sees.
        found = choices(build(tmp_path, one_db(PG)))[0]
        with pytest.raises(dataclasses.FrozenInstanceError):
            found.vendor = Vendor.SQLITE  # type: ignore[misc]

    def test_nothing_is_not_sqlite(self, tmp_path: pathlib.Path) -> None:
        # The presence control for the assertion above: an empty result must
        # not be reachable by any path that also produces a vendor.
        assert all(c.vendor is not Vendor.UNKNOWN for c in choices(build(tmp_path, one_db(PG))))
        assert choices(build(tmp_path, "")) == ()


class TestReadingAWholeProject:
    """What `project_engines` gathers across modules."""

    def test_engines_from_separate_modules_are_gathered(self, tmp_path: pathlib.Path) -> None:
        # The shape the plan assumed. It is real, just not what the corpus does.
        root = build(tmp_path, one_db(PG), dev=one_db(LITE))
        found = {(c.module.dotted, c.vendor) for c in choices(root)}
        assert found == {
            ("config.settings", Vendor.POSTGRESQL),
            ("config.dev", Vendor.SQLITE),
        }

    def test_choices_are_ordered_by_line(self, tmp_path: pathlib.Path) -> None:
        body = one_db(LITE) + "import os\nif os.getenv('DB') == 'postgres':\n    " + one_db(PG)
        found = module_engines_for(tmp_path, body)
        assert [c.line for c in found] == sorted(c.line for c in found)
        assert found[0].line < found[1].line

    def test_describe_names_the_engine_and_where_it_was_written(
        self, tmp_path: pathlib.Path
    ) -> None:
        found = choices(build(tmp_path, one_db(PG)))
        assert found[0].describe() == f"{PG} at config.settings:{found[0].line}"

    def test_describe_admits_when_the_engine_is_unreadable(self, tmp_path: pathlib.Path) -> None:
        body = (
            "import os\n"
            "DATABASES = {'default': {'ENGINE': 'django.db.backends.' + os.environ['B']}}\n"
        )
        assert "cannot read" in choices(build(tmp_path, body))[0].describe()


def module_engines_for(tmp_path: pathlib.Path, body: str) -> tuple[EngineChoice, ...]:
    root = build(tmp_path, body)
    ctx = build_context(root)
    views = resolve_all(ctx)
    module = next(m for m in ctx.settings_modules if m.dotted == "config.settings")
    return module_engines(module, views[module.dotted])


class TestGuardRecovery:
    """The `if` test that selects a branch, recovered for evidence."""

    def test_an_unconditional_assignment_has_no_guard(self, tmp_path: pathlib.Path) -> None:
        found = choices(build(tmp_path, one_db(PG)))[0]
        assert found.guard is None
        assert found.selected_by() == "with nothing set"

    def test_the_env_test_is_recovered_verbatim(self, tmp_path: pathlib.Path) -> None:
        # The Healthchecks lever. Without this a finding can only say "it is
        # conditional", which tells a reader to go and look rather than what
        # to set.
        body = one_db(LITE) + "import os\nif os.getenv('DB') == 'postgres':\n    " + one_db(PG)
        found = choices(build(tmp_path, body))[1]
        assert found.guard == "os.getenv('DB') == 'postgres'"
        assert found.selected_by() == "when os.getenv('DB') == 'postgres'"

    def test_nested_guards_are_joined_because_both_must_hold(self, tmp_path: pathlib.Path) -> None:
        body = (
            one_db(LITE)
            + "import os\nif os.getenv('DB'):\n    if os.getenv('DB') == 'postgres':\n        "
            + one_db(PG)
        )
        assert choices(build(tmp_path, body))[1].guard == (
            "os.getenv('DB') and os.getenv('DB') == 'postgres'"
        )

    def test_an_else_branch_is_recorded_negated(self, tmp_path: pathlib.Path) -> None:
        body = (
            "import os\nif os.getenv('DB') == 'postgres':\n    "
            + one_db(PG)
            + "else:\n    "
            + one_db(LITE)
        )
        found = choices(build(tmp_path, body))
        assert found[1].guard == "not (os.getenv('DB') == 'postgres')"

    def test_a_conditional_without_a_recoverable_guard_still_says_so(
        self, tmp_path: pathlib.Path
    ) -> None:
        # A guard we cannot name must not read as "with nothing set", which is
        # the opposite claim.
        root = build(tmp_path, one_db(PG))
        ctx = build_context(root)
        views = resolve_all(ctx)
        module = ctx.settings_modules[0]
        bare = module_engines(module, views[module.dotted], tree=None)[0]
        assert dataclasses.replace(bare, conditional=True).selected_by() == (
            "on a branch we could not name"
        )


class TestDecidingDivergence:
    """`divergence` over a set of choices."""

    def verdict(self, tmp_path: pathlib.Path, body: str, **extra: str) -> Divergence:
        return divergence(choices(build(tmp_path, body, **extra)))

    def test_one_engine_is_not_divergence(self, tmp_path: pathlib.Path) -> None:
        found = self.verdict(tmp_path, one_db(PG))
        assert found.verdict is Portability.SINGLE
        assert not found.diverges
        assert found.vendors == frozenset({Vendor.POSTGRESQL})

    def test_the_same_engine_twice_is_not_divergence(self, tmp_path: pathlib.Path) -> None:
        # Two branches that agree are a deployment detail, not a portability
        # problem, and reporting them would fire on every project that
        # overrides NAME per environment.
        body = one_db(PG) + "import os\nif os.getenv('X'):\n    " + one_db(PG)
        assert self.verdict(tmp_path, body).verdict is Portability.SINGLE

    def test_sqlite_by_default_and_postgres_on_demand_diverges(
        self, tmp_path: pathlib.Path
    ) -> None:
        body = one_db(LITE) + "import os\nif os.getenv('DB') == 'postgres':\n    " + one_db(PG)
        found = self.verdict(tmp_path, body)
        assert found.verdict is Portability.DIVERGENT
        assert found.alias == "default"
        assert found.default is not None
        assert found.default.vendor is Vendor.SQLITE
        assert [a.vendor for a in found.alternatives] == [Vendor.POSTGRESQL]

    def test_engines_split_across_modules_diverge_too(self, tmp_path: pathlib.Path) -> None:
        # The shape the plan assumed. Rarer than the one above, still real.
        found = self.verdict(tmp_path, one_db(PG), dev=one_db(LITE))
        assert found.verdict is Portability.DIVERGENT
        assert found.vendors == frozenset({Vendor.POSTGRESQL, Vendor.SQLITE})

    def test_an_unreadable_engine_is_uncertain_not_single(self, tmp_path: pathlib.Path) -> None:
        body = (
            "import os\n"
            "DATABASES = {'default': {'ENGINE': 'django.db.backends.' + os.environ['B']}}\n"
        )
        found = self.verdict(tmp_path, body)
        assert found.verdict is Portability.UNCERTAIN
        assert not found.conclusive

    def test_no_engine_at_all_is_unreadable_not_single(self, tmp_path: pathlib.Path) -> None:
        found = self.verdict(tmp_path, "")
        assert found.verdict is Portability.UNREADABLE
        assert not found.conclusive
        assert found.vendors == frozenset()

    def test_a_readable_divergence_beats_an_unreadable_sibling(
        self, tmp_path: pathlib.Path
    ) -> None:
        # Proven divergence is not weakened by a third branch we cannot read:
        # the two we can read already answer the question.
        body = (
            one_db(LITE)
            + "import os\nif os.getenv('DB') == 'postgres':\n    "
            + one_db(PG)
            + "if os.getenv('DB') == 'other':\n    "
            + "DATABASES = {'default': {'ENGINE': 'django.db.backends.' + os.environ['B']}}\n"
        )
        assert self.verdict(tmp_path, body).verdict is Portability.DIVERGENT

    def test_two_aliases_with_different_engines_are_not_this_defect(
        self, tmp_path: pathlib.Path
    ) -> None:
        # A Postgres default beside a SQLite replica is a real problem and a
        # different one. Folding it in here would report each as the other.
        body = (
            f"DATABASES = {{'default': {{'ENGINE': '{PG}'}}, 'replica': {{'ENGINE': '{LITE}'}}}}\n"
        )
        found = self.verdict(tmp_path, body)
        assert found.verdict is Portability.SINGLE
        assert found.vendors == frozenset({Vendor.POSTGRESQL, Vendor.SQLITE})


class TestAskingWhichVendorRuns:
    """`reaches` is evidence; `could_reach` is possibility."""

    def verdict(self, tmp_path: pathlib.Path, body: str) -> Divergence:
        return divergence(choices(build(tmp_path, body)))

    def test_a_readable_engine_is_reached(self, tmp_path: pathlib.Path) -> None:
        found = self.verdict(tmp_path, one_db(PG))
        assert found.reaches(Vendor.POSTGRESQL)
        assert not found.reaches(Vendor.SQLITE)

    def test_an_unreadable_engine_is_not_reached_but_is_not_excluded(
        self, tmp_path: pathlib.Path
    ) -> None:
        # The distinction that keeps this family off NetBox and pretix, whose
        # engines cannot be read. A rule firing on `could_reach` would report
        # every SQLite divergence against two Postgres-only projects.
        body = (
            "import os\n"
            "DATABASES = {'default': {'ENGINE': 'django.db.backends.' + os.environ['B']}}\n"
        )
        found = self.verdict(tmp_path, body)
        assert not found.reaches(Vendor.SQLITE)
        assert found.could_reach(Vendor.SQLITE)

    def test_a_conclusive_single_engine_excludes_the_others(self, tmp_path: pathlib.Path) -> None:
        # The presence control for the test above: when everything was
        # readable, `could_reach` must stop saying yes to everything.
        found = self.verdict(tmp_path, one_db(PG))
        assert found.conclusive
        assert not found.could_reach(Vendor.SQLITE)
        assert found.could_reach(Vendor.POSTGRESQL)


class TestNarrowingToTheDivergingAlias:
    """`relevant`, `default` and `alternatives` on a `Divergence`."""

    def verdict(self, tmp_path: pathlib.Path, body: str) -> Divergence:
        return divergence(choices(build(tmp_path, body)))

    def test_only_the_diverging_alias_is_described(self, tmp_path: pathlib.Path) -> None:
        # A second alias must not turn up in the finding about the first, or a
        # reader is sent to a line that is not the problem.
        body = (
            f"DATABASES = {{'default': {{'ENGINE': '{LITE}'}}, "
            f"'other': {{'ENGINE': '{MY}'}}}}\n"
            "import os\nif os.getenv('DB') == 'postgres':\n    "
            f"DATABASES = {{'default': {{'ENGINE': '{PG}'}}}}\n"
        )
        found = self.verdict(tmp_path, body)
        assert found.alias == "default"
        assert {c.alias for c in found.relevant} == {"default"}
        assert {c.alias for c in found.choices} == {"default", "other"}

    def test_without_a_diverging_alias_everything_is_relevant(self, tmp_path: pathlib.Path) -> None:
        # The contrast: when nothing diverges there is no alias to narrow to,
        # so narrowing must not silently drop the choices.
        found = self.verdict(tmp_path, one_db(PG))
        assert found.alias is None
        assert found.relevant == found.choices

    def test_a_branch_agreeing_with_the_default_is_not_an_alternative(
        self, tmp_path: pathlib.Path
    ) -> None:
        # Healthchecks' Postgres branch is an alternative to its SQLite
        # default; a second SQLite branch is the same database again.
        body = (
            one_db(LITE)
            + "import os\nif os.getenv('X'):\n    "
            + one_db(LITE)
            + "if os.getenv('DB') == 'postgres':\n    "
            + one_db(PG)
        )
        found = self.verdict(tmp_path, body)
        assert found.diverges
        assert [a.vendor for a in found.alternatives] == [Vendor.POSTGRESQL]

    def test_with_no_unconditional_assignment_there_is_no_default(
        self, tmp_path: pathlib.Path
    ) -> None:
        # Every branch is opt-in, so there is nothing to call the developer's
        # database and nothing to compare the others against.
        body = (
            "import os\nif os.getenv('DB') == 'postgres':\n    "
            + one_db(PG)
            + "if os.getenv('DB') == 'mysql':\n    "
            + one_db(MY)
        )
        found = self.verdict(tmp_path, body)
        assert found.diverges
        assert found.default is None
        assert found.alternatives == ()

    def test_the_verdict_names_are_the_ones_findings_will_print(self) -> None:
        assert [str(v) for v in Portability] == [
            "single",
            "divergent",
            "uncertain",
            "unreadable",
        ]

    def test_a_verdict_cannot_be_edited_after_it_is_built(self, tmp_path: pathlib.Path) -> None:
        found = self.verdict(tmp_path, one_db(PG))
        with pytest.raises(dataclasses.FrozenInstanceError):
            found.verdict = Portability.DIVERGENT  # type: ignore[misc]


class TestChoosingTheDevelopersDatabase:
    """`default` when conditionality cannot decide it."""

    def test_a_development_module_wins_over_a_production_one(self, tmp_path: pathlib.Path) -> None:
        # Both assignments are unconditional, so `conditional` says nothing.
        # The role does: a module that does not reach production is the
        # developer's by definition.
        found = divergence(choices(build(tmp_path, one_db(PG), dev=one_db(LITE))))
        assert found.default is not None
        assert found.default.vendor is Vendor.SQLITE
        assert found.default.module.dotted == "config.dev"
        assert [a.vendor for a in found.alternatives] == [Vendor.POSTGRESQL]

    def test_an_unconditional_engine_in_another_module_is_an_alternative(
        self, tmp_path: pathlib.Path
    ) -> None:
        # Requiring an alternative to be conditional made this return nothing
        # for the one shape the plan set out to catch.
        found = divergence(choices(build(tmp_path, one_db(PG), dev=one_db(LITE))))
        assert found.alternatives != ()
        assert all(a.default for a in found.alternatives)


class TestNamingAModule:
    """`module_name`, the one place an unnamed module is handled."""

    def test_a_dotted_name_is_used_when_there_is_one(self) -> None:
        module = SettingsModule(
            path=pathlib.Path("/p/config/settings.py"),
            dotted="config.settings",
            role=SettingsRole.PRODUCTION,
        )
        assert module_name(module) == "config.settings"

    def test_a_root_package_falls_back_to_its_filename(self) -> None:
        # A settings module that is its tree's root resolves to an empty
        # dotted name, and "the 'default' database in  is ..." names nothing.
        module = SettingsModule(
            path=pathlib.Path("/p/__init__.py"), dotted="", role=SettingsRole.PRODUCTION
        )
        assert module_name(module) == "__init__.py"

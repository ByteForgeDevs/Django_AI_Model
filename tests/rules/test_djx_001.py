"""DJX-001 -- development and production run different database engines.

The test that matters most structurally is
`test_a_development_module_is_half_the_finding_not_an_exemption`. Every other
settings rule in this project ignores modules whose role does not reach
production, because a development module is *supposed* to have `DEBUG = True`
and reporting it is how a tool teaches people to ignore it. This rule inverts
that: the development module is not an exception to the finding, it is one half
of it, and a rule built on `SettingsRule` would have silently dropped the
Postgres-in-prod / SQLite-in-dev shape entirely.
"""

from __future__ import annotations

import pathlib

from djaudit import engine
from djaudit.models import Finding

MARKERS = "INSTALLED_APPS = []\nSECRET_KEY = 'x'\nDEBUG = False\n"

LITE = "django.db.backends.sqlite3"
PG = "django.db.backends.postgresql"
MY = "django.db.backends.mysql"
ORA = "django.db.backends.oracle"


def one_db(name: str) -> str:
    return f"DATABASES = {{'default': {{'ENGINE': '{name}', 'NAME': 'app'}}}}\n"


def build(tmp_path: pathlib.Path, body: str, **extra: str) -> pathlib.Path:
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


def findings(root: pathlib.Path) -> list[Finding]:
    """Every DJX-001 finding, having first checked the rule did not crash.

    The engine catches a rule's exceptions and records them, by design, so a
    rule that raises reports nothing rather than taking the run down with it.
    That makes an unguarded `== []` assertion pass for a rule that never ran,
    which is the opposite of what such a test is written to say.
    """
    result = engine.run(root)
    assert "DJX-001" not in result.rule_errors, result.rule_errors.get("DJX-001")
    return [f for f in result.findings if f.rule_id == "DJX-001"]


HEALTHCHECKS = one_db(LITE) + "import os\nif os.getenv('DB') == 'postgres':\n    " + one_db(PG)
"""The shape Healthchecks uses, reduced to its essentials."""


class TestWhenItFires:
    def test_sqlite_by_default_and_postgres_on_demand_is_reported(
        self, tmp_path: pathlib.Path
    ) -> None:
        found = findings(build(tmp_path, HEALTHCHECKS))
        assert len(found) == 1
        assert found[0].severity.value == "medium"
        assert found[0].confidence.value == "certain"

    def test_the_message_names_both_engines_and_the_lever(self, tmp_path: pathlib.Path) -> None:
        # "it is conditional" sends a reader to go and look. The environment
        # variable is the part they can act on.
        message = findings(build(tmp_path, HEALTHCHECKS))[0].message
        assert LITE in message
        assert PG in message
        assert "with nothing set" in message
        assert "when os.getenv('DB') == 'postgres'" in message

    def test_three_engines_are_still_one_finding(self, tmp_path: pathlib.Path) -> None:
        # Healthchecks reaches three. One decision, one finding.
        body = HEALTHCHECKS + "if os.getenv('DB') in ['mysql', 'mariadb']:\n    " + one_db(MY)
        found = findings(build(tmp_path, body))
        assert len(found) == 1
        assert MY in found[0].message
        assert PG in found[0].message

    def test_it_points_at_the_database_a_developer_gets(self, tmp_path: pathlib.Path) -> None:
        # The SQLite half, because that is the assignment whose consequence is
        # invisible -- the Postgres branch is the one somebody opted into.
        found = findings(build(tmp_path, HEALTHCHECKS))[0]
        assert found.location.file.endswith("settings.py")
        assert LITE in found.location.snippet

    def test_a_development_module_is_half_the_finding_not_an_exemption(
        self, tmp_path: pathlib.Path
    ) -> None:
        # The shape the plan assumed, and the reason this rule does not build
        # on SettingsRule: that base skips modules that do not reach
        # production, which is exactly where the development database lives.
        # Neither assignment is conditional, so conditionality cannot say
        # which one a developer gets -- the module's role has to.
        found = findings(build(tmp_path, one_db(PG), dev=one_db(LITE)))
        assert len(found) == 1
        assert found[0].location.file.endswith("dev.py")
        assert f"is {LITE} with nothing set" in found[0].message
        assert f"and {PG} in config.settings" in found[0].message

    def test_the_evidence_lists_every_branch_with_how_it_is_reached(
        self, tmp_path: pathlib.Path
    ) -> None:
        found = findings(build(tmp_path, HEALTHCHECKS))[0]
        config = [e for e in found.evidence if e.kind.value == "config"]
        assert len(config) == 1
        assert "reached with nothing set" in config[0].content
        assert "reached when os.getenv('DB') == 'postgres'" in config[0].content

    def test_the_properties_carry_the_alias_and_the_vendors(self, tmp_path: pathlib.Path) -> None:
        found = findings(build(tmp_path, HEALTHCHECKS))[0]
        assert found.properties["alias"] == "default"
        assert found.properties["vendors"] == "postgresql sqlite"


class TestWhenItStaysSilent:
    def test_one_engine_everywhere_is_not_reported(self, tmp_path: pathlib.Path) -> None:
        assert findings(build(tmp_path, one_db(PG))) == []

    def test_two_branches_naming_the_same_engine_are_not_reported(
        self, tmp_path: pathlib.Path
    ) -> None:
        # Overriding NAME or HOST per environment is normal configuration, not
        # a portability problem, and reporting it would fire on almost
        # everything.
        body = one_db(PG) + "import os\nif os.getenv('X'):\n    " + one_db(PG)
        assert findings(build(tmp_path, body)) == []

    def test_an_unreadable_engine_is_not_reported(self, tmp_path: pathlib.Path) -> None:
        # The pretix shape. We cannot say what it runs, so we do not.
        body = (
            "import os\n"
            "DATABASES = {'default': {'ENGINE': 'django.db.backends.' + os.environ['B']}}\n"
        )
        assert findings(build(tmp_path, body)) == []

    def test_an_unreadable_engine_beside_a_readable_one_is_not_reported(
        self, tmp_path: pathlib.Path
    ) -> None:
        # One known vendor and one unknown is not two known vendors. Guessing
        # that the unreadable branch is a different engine is how this family
        # would start reporting on projects it cannot read.
        body = (
            one_db(PG)
            + "import os\nif os.getenv('DB'):\n    "
            + "DATABASES = {'default': {'ENGINE': 'django.db.backends.' + os.environ['B']}}\n"
        )
        assert findings(build(tmp_path, body)) == []

    def test_a_project_with_no_databases_is_not_reported(self, tmp_path: pathlib.Path) -> None:
        assert findings(build(tmp_path, "")) == []

    def test_two_aliases_with_different_engines_are_not_this_finding(
        self, tmp_path: pathlib.Path
    ) -> None:
        # A Postgres default beside a SQLite replica is a real defect and a
        # different one. This rule is about one connection changing engine.
        body = (
            f"DATABASES = {{'default': {{'ENGINE': '{PG}'}}, 'replica': {{'ENGINE': '{LITE}'}}}}\n"
        )
        assert findings(build(tmp_path, body)) == []

    def test_with_every_branch_conditional_there_is_no_development_half(
        self, tmp_path: pathlib.Path
    ) -> None:
        # Nothing runs when nothing is set, so there is no "what a developer
        # gets" to contrast the deployments against.
        body = (
            "import os\nif os.getenv('DB') == 'postgres':\n    "
            + one_db(PG)
            + "if os.getenv('DB') == 'mysql':\n    "
            + one_db(MY)
        )
        assert findings(build(tmp_path, body)) == []


class TestWhenNoEngineIsTheDefault:
    """Divergence with no unconditional branch: real, and not this finding."""

    def test_two_opt_in_branches_are_not_reported(self, tmp_path: pathlib.Path) -> None:
        # Both engines are behind an `if`, so no engine is what a developer
        # gets by doing nothing and there is no development half to contrast.
        # DJX-001 is about the dev/prod split specifically, and reporting this
        # would be reporting a different, weaker claim under its ID.
        body = (
            "import os\nif os.getenv('DB') == 'lite':\n    "
            + one_db(LITE)
            + "if os.getenv('DB') == 'pg':\n    "
            + one_db(PG)
        )
        assert findings(build(tmp_path, body)) == []

    def test_the_same_shape_with_a_fallback_is_reported(self, tmp_path: pathlib.Path) -> None:
        # The control for the test above: the only difference is that one
        # branch is unconditional, so silence there is about the default and
        # not about djaudit failing to read two conditional assignments.
        body = one_db(LITE) + "import os\nif os.getenv('DB') == 'pg':\n    " + one_db(PG)
        assert len(findings(build(tmp_path, body))) == 1


class TestItIsStillTheSameRule:
    def test_the_annotated_spelling_is_read(self, tmp_path: pathlib.Path) -> None:
        # Healthchecks writes `DATABASES: Mapping[str, Any] = {...}`. Reading
        # only bare assignments would find no database there at all.
        body = (
            f"DATABASES: dict = {{'default': {{'ENGINE': '{LITE}'}}}}\n"
            "import os\nif os.getenv('DB') == 'postgres':\n    " + one_db(PG)
        )
        assert len(findings(build(tmp_path, body))) == 1

    def test_a_gis_backend_is_read_through_to_its_vendor(self, tmp_path: pathlib.Path) -> None:
        # spatialite is SQLite and postgis is Postgres, so this is the same
        # divergence written in the GIS spelling -- and neither name contains
        # the vendor it belongs to.
        body = (
            one_db("django.contrib.gis.db.backends.spatialite")
            + "import os\nif os.getenv('DB'):\n    "
            + one_db("django.contrib.gis.db.backends.postgis")
        )
        found = findings(build(tmp_path, body))
        assert len(found) == 1
        assert found[0].properties["vendors"] == "postgresql sqlite"

    def test_the_same_vendor_in_two_spellings_is_not_divergence(
        self, tmp_path: pathlib.Path
    ) -> None:
        # The control for the test above: postgis and postgresql are one
        # database, so recognising them must not turn into reporting them.
        body = (
            one_db(PG)
            + "import os\nif os.getenv('GIS'):\n    "
            + one_db("django.contrib.gis.db.backends.postgis")
        )
        assert findings(build(tmp_path, body)) == []


class TestHowItReads:
    """The message and properties, which are the whole product of a finding."""

    def test_three_engines_are_listed_as_a_series(self, tmp_path: pathlib.Path) -> None:
        # Healthchecks' actual shape. Two alternatives joined by a bare space
        # would run the engine names together into one unreadable string.
        body = (
            HEALTHCHECKS
            + "if os.getenv('DB') == 'mysql':\n    "
            + one_db(MY)
            + "if os.getenv('DB') == 'oracle':\n    "
            + one_db(ORA)
        )
        found = findings(build(tmp_path, body))
        assert len(found) == 1
        assert (
            f"the 'default' database in config.settings is {LITE} with nothing set "
            f"and {PG} when os.getenv('DB') == 'postgres', "
            f"{MY} when os.getenv('DB') == 'mysql' "
            f"or {ORA} when os.getenv('DB') == 'oracle', so" in found[0].message
        )

    def test_the_engine_model_is_shown_as_its_own_evidence(self, tmp_path: pathlib.Path) -> None:
        # The snippet shows one assignment. What a reader needs in order to
        # disagree is the whole set djaudit derived, and where it came from.
        found = findings(build(tmp_path, HEALTHCHECKS))
        model = [e for e in found[0].evidence if e.source == "djaudit database engine model"]
        assert len(model) == 1
        assert f"{LITE} at config.settings:4 -- reached with nothing set" in model[0].content
        assert f"{PG} at config.settings:7 -- reached when os.getenv" in model[0].content

    def test_it_carries_the_module_it_fired_on(self, tmp_path: pathlib.Path) -> None:
        # A consumer grouping findings by settings module cannot recover either
        # of these from the location alone.
        found = findings(build(tmp_path, one_db(PG), dev=one_db(LITE)))
        assert found[0].properties["settings_module"] == "config.dev"
        assert found[0].properties["settings_role"] == "development"

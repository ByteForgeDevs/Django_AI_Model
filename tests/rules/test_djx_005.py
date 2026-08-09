"""DJX-005 -- a `django.contrib.postgres` field in a project that also runs SQLite.

The bluntest rule in the family. DJX-004 reports a query that answers
differently; this reports a schema one of the two engines cannot hold at all.

Measured against a real SQLite before it was written, one model per field type:

    ArrayField           migrate fails: near "[]": syntax error
    HStoreField          table created, first write: type 'dict' is not supported
    IntegerRangeField    table created, first write: unrecognized token ":"
    DateTimeRangeField   table created, first write: unrecognized token ":"
    SearchVectorField    table created, NULL round-trips; SearchVector needs
                         to_tsvector, which SQLite does not have, and `@@` is a
                         syntax error there

Three different outcomes, which is why the rule says which one applies rather
than one sentence for all of them. The same models on the live Postgres
created, wrote, read back and filtered without complaint.
"""

from __future__ import annotations

import pathlib

import pytest

from djaudit import engine
from djaudit.models import Finding

MARKERS = (
    "SECRET_KEY = 'x'\nDEBUG = False\nALLOWED_HOSTS = ['example.com']\nINSTALLED_APPS = ['shop']\n"
)

LITE = "django.db.backends.sqlite3"
PG = "django.db.backends.postgresql"

DIVERGENT = (
    f"DATABASES = {{'default': {{'ENGINE': '{LITE}'}}}}\n"
    "import os\n"
    "if os.getenv('DB') == 'pg':\n"
    f"    DATABASES = {{'default': {{'ENGINE': '{PG}'}}}}\n"
)

POSTGRES_ONLY = f"DATABASES = {{'default': {{'ENGINE': '{PG}'}}}}\n"

ARRAY = (
    "from django.contrib.postgres.fields import ArrayField\n"
    "from django.db import models\n\n\n"
    "class Order(models.Model):\n"
    "    sku = models.CharField(max_length=32)\n"
    "    tags = ArrayField(models.CharField(max_length=16), default=list)\n"
)

PORTABLE = (
    "from django.db import models\n\n\n"
    "class Order(models.Model):\n"
    "    sku = models.CharField(max_length=32)\n"
    "    tags = models.JSONField(default=list)\n"
)


def build(tmp_path: pathlib.Path, databases: str, models: str) -> pathlib.Path:
    root = tmp_path / f"proj{len(list(tmp_path.iterdir()))}"
    (root / "config").mkdir(parents=True)
    (root / "shop").mkdir()
    (root / "manage.py").write_text(
        "import os\nos.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')\n"
    )
    (root / "config" / "__init__.py").write_text("")
    (root / "config" / "settings.py").write_text(MARKERS + databases)
    (root / "shop" / "__init__.py").write_text("")
    (root / "shop" / "models.py").write_text(models)
    return root


def findings(root: pathlib.Path) -> list[Finding]:
    """Every DJX-005 finding, having first checked the rule did not crash."""
    result = engine.run(root)
    assert "DJX-005" not in result.rule_errors, result.rule_errors.get("DJX-005")
    return [f for f in result.findings if f.rule_id == "DJX-005"]


def model(body: str, imports: str = "from django.contrib.postgres.fields import ArrayField") -> str:
    return f"{imports}\nfrom django.db import models\n\n\nclass Order(models.Model):\n{body}"


class TestWhenItFires:
    def test_an_array_field_is_reported(self, tmp_path: pathlib.Path) -> None:
        found = findings(build(tmp_path, DIVERGENT, ARRAY))
        assert len(found) == 1
        assert found[0].severity.value == "high"
        assert found[0].confidence.value == "certain"

    def test_it_points_at_the_field_not_the_class(self, tmp_path: pathlib.Path) -> None:
        # The line that has to change is the declaration. A finding on the
        # `class` line makes a model with two offending fields ambiguous.
        found = findings(build(tmp_path, DIVERGENT, ARRAY))
        assert found[0].location.line == 7
        assert "ArrayField" in (found[0].location.snippet or "")

    def test_the_message_names_the_model_the_field_and_the_type(
        self, tmp_path: pathlib.Path
    ) -> None:
        message = findings(build(tmp_path, DIVERGENT, ARRAY))[0].message
        assert "`Order.tags`" in message
        assert "uses ArrayField" in message
        assert "which only Postgres has" in message

    def test_the_resolved_import_path_is_the_evidence(self, tmp_path: pathlib.Path) -> None:
        # The whole judgement rests on where the class came from, not on its
        # name, so the resolved path is what has to be shown.
        found = findings(build(tmp_path, DIVERGENT, ARRAY))
        graph = [e for e in found[0].evidence if e.source == "model graph"]
        assert len(graph) == 1
        assert "django.contrib.postgres.fields.ArrayField" in graph[0].content
        assert "resolved through this module" in graph[0].content

    def test_the_field_type_is_carried_as_a_property(self, tmp_path: pathlib.Path) -> None:
        # So a consumer can group by type without parsing the message.
        found = findings(build(tmp_path, DIVERGENT, ARRAY))
        assert found[0].properties["kind"] == "ArrayField"

    def test_it_says_that_migrate_itself_fails(self, tmp_path: pathlib.Path) -> None:
        # Measured. This is the distinguishing fact about ArrayField and the
        # reason it is worse than the rest: the table never exists, so nothing
        # about the project works on SQLite, not merely the array.
        message = findings(build(tmp_path, DIVERGENT, ARRAY))[0].message
        assert "migrate` fails" in message
        assert '[]": syntax error' in message

    def test_an_hstore_field_fails_later_and_says_so(self, tmp_path: pathlib.Path) -> None:
        # Also measured, and a genuinely different outcome: SQLite accepts any
        # word as a column type, so the table is created and the developer
        # meets the failure on the first write instead of on migrate.
        code = model(
            "    data = HStoreField(default=dict)\n",
            imports="from django.contrib.postgres.fields import HStoreField",
        )
        found = findings(build(tmp_path, DIVERGENT, code))
        assert len(found) == 1
        assert "the table is created" in found[0].message
        assert "'dict' is not supported" in found[0].message

    @pytest.mark.parametrize(
        ("kind", "sql_type"),
        [
            ("IntegerRangeField", "int4range"),
            ("BigIntegerRangeField", "int8range"),
            ("DecimalRangeField", "numrange"),
            ("DateRangeField", "daterange"),
            ("DateTimeRangeField", "tstzrange"),
        ],
    )
    def test_every_range_field_names_its_own_postgres_type(
        self, tmp_path: pathlib.Path, kind: str, sql_type: str
    ) -> None:
        # Five near-identical entries in the effects table, and a table entry
        # no test reads is a claim nobody checked. Naming the type each one
        # renders is what makes them five entries rather than one.
        code = model(
            f"    window = {kind}(null=True)\n",
            imports=f"from django.contrib.postgres.fields import {kind}",
        )
        found = findings(build(tmp_path, DIVERGENT, code))
        assert len(found) == 1
        assert sql_type in found[0].message
        assert "the first write fails" in found[0].message

    def test_a_search_vector_field_fails_last_of_all(self, tmp_path: pathlib.Path) -> None:
        # The quietest of the three outcomes: measured, SQLite creates the
        # column and stores NULL in it happily. Nothing fails until a search
        # runs, which is exactly the code path a developer adds last.
        code = model(
            "    search = SearchVectorField(null=True)\n",
            imports="from django.contrib.postgres.search import SearchVectorField",
        )
        found = findings(build(tmp_path, DIVERGENT, code))
        assert len(found) == 1
        assert "to_tsvector" in found[0].message

    def test_two_offending_fields_are_two_findings(self, tmp_path: pathlib.Path) -> None:
        # Not grouped per model, because the replacement differs by type: an
        # ArrayField becomes a JSONField and a range becomes two columns and a
        # constraint. One finding would have two different fixes.
        code = model(
            "    tags = ArrayField(models.CharField(max_length=16), default=list)\n"
            "    window = DateTimeRangeField(null=True)\n",
            imports=("from django.contrib.postgres.fields import ArrayField, DateTimeRangeField"),
        )
        found = findings(build(tmp_path, DIVERGENT, code))
        assert len(found) == 2
        assert {f.properties["field"] for f in found} == {"Order.tags", "Order.window"}
        assert {f.location.line for f in found} == {6, 7}

    def test_an_unfamiliar_postgres_field_is_still_reported(self, tmp_path: pathlib.Path) -> None:
        # The package decides, not a list of class names. A list would report
        # nothing the first time Django adds a field while still looking like
        # it worked.
        code = model(
            "    thing = SomeFutureField()\n",
            imports="from django.contrib.postgres.fields import SomeFutureField",
        )
        found = findings(build(tmp_path, DIVERGENT, code))
        assert len(found) == 1
        assert "no equivalent type" in found[0].message

    def test_an_aliased_import_is_still_resolved(self, tmp_path: pathlib.Path) -> None:
        code = model(
            "    tags = Arr(models.CharField(max_length=16), default=list)\n",
            imports="from django.contrib.postgres.fields import ArrayField as Arr",
        )
        assert len(findings(build(tmp_path, DIVERGENT, code))) == 1

    def test_a_module_import_is_still_resolved(self, tmp_path: pathlib.Path) -> None:
        code = model(
            "    tags = pg.ArrayField(models.CharField(max_length=16), default=list)\n",
            imports="from django.contrib.postgres import fields as pg",
        )
        assert len(findings(build(tmp_path, DIVERGENT, code))) == 1


class TestWhenItStaysSilent:
    def test_a_postgres_only_project_is_not_reported(self, tmp_path: pathlib.Path) -> None:
        # An ArrayField in a project that only ever runs Postgres is a correct
        # use of the type, not a defect.
        assert findings(build(tmp_path, POSTGRES_ONLY, ARRAY)) == []

    def test_the_divergence_is_what_makes_it_reportable(self, tmp_path: pathlib.Path) -> None:
        # The presence control for the test above.
        assert len(findings(build(tmp_path, DIVERGENT, ARRAY))) == 1

    def test_a_portable_field_is_not_reported(self, tmp_path: pathlib.Path) -> None:
        assert findings(build(tmp_path, DIVERGENT, PORTABLE)) == []

    def test_the_core_json_field_is_not_a_postgres_field(self, tmp_path: pathlib.Path) -> None:
        # `django.db.models.JSONField` runs on both backends -- it is the
        # remediation this rule recommends. Only its containment *lookups*
        # diverge, and that is DJX-002.
        code = model(
            "    attributes = models.JSONField(default=dict)\n",
            imports="from django.db import models as _m",
        )
        assert findings(build(tmp_path, DIVERGENT, code)) == []

    def test_a_project_field_that_merely_shares_the_name_is_not_reported(
        self, tmp_path: pathlib.Path
    ) -> None:
        # A project's own `ArrayField` is not Django's. Matching the class name
        # would report someone else's portable field as a Postgres dependency.
        code = model(
            "    tags = ArrayField()\n",
            imports="from shop.custom import ArrayField",
        )
        root = build(tmp_path, DIVERGENT, code)
        (root / "shop" / "custom.py").write_text(
            "from django.db import models\n\n\nclass ArrayField(models.TextField):\n    pass\n"
        )
        assert findings(root) == []

    def test_a_proxy_model_does_not_repeat_its_parent_s_fields(
        self, tmp_path: pathlib.Path
    ) -> None:
        # A proxy shares its parent's table, so reporting it would be reporting
        # the same column twice under two names.
        code = ARRAY + ("\n\nclass OrderProxy(Order):\n    class Meta:\n        proxy = True\n")
        found = findings(build(tmp_path, DIVERGENT, code))
        assert len(found) == 1
        assert found[0].properties["field"] == "Order.tags"


class TestItIsStillTheSameRule:
    def test_it_is_registered_under_the_portability_family(self) -> None:
        from djaudit.models import Family
        from djaudit.registry import get

        assert get("DJX-005").meta.family is Family.DJX

    def test_it_admits_it_cannot_see_a_model_built_by_a_factory(self) -> None:
        from djaudit.registry import get

        assert any("factory" in limit for limit in get("DJX-005").meta.limitations)

    def test_it_admits_it_says_nothing_about_third_party_fields(self) -> None:
        from djaudit.registry import get

        assert any("third-party" in limit for limit in get("DJX-005").meta.limitations)

    def test_it_records_what_the_divergence_gate_buys(self) -> None:
        # Measured, not asserted: NetBox declares 23 of these fields on models
        # and DJX-005 reports none of them, because NetBox has never run
        # SQLite. That number is the family's precision argument.
        from djaudit.registry import get

        assert any("NetBox declares 23" in limit for limit in get("DJX-005").meta.limitations)

    def test_the_remediation_names_a_portable_replacement(self) -> None:
        from djaudit.registry import get

        assert "JSONField" in get("DJX-005").meta.remediation

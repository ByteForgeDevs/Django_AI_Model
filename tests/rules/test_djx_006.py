"""DJX-006 -- constraints SQLite declines to create, or cannot parse at all.

The most dangerous rule in the family, because it is the only one whose
consequence is *data*. Everything else makes a query answer differently or a
migration fail. This one lets a developer's database hold rows production would
have refused.

Measured against a real SQLite and the live Postgres before it was written.
The emitted schema, same model, one argument apart:

    UniqueConstraint(fields=["sku"], name="x")
        sqlite   CREATE TABLE ... , CONSTRAINT "x" UNIQUE ("sku"))
    UniqueConstraint(fields=["sku"], name="x", deferrable=DEFERRED)
        sqlite   CREATE TABLE ... "sku" varchar(32) NOT NULL)      <- gone

And the consequence, inserting the same value twice:

    deferred    sqlite ACCEPTED (two rows)   postgres IntegrityError
    immediate   sqlite ACCEPTED (two rows)   postgres IntegrityError
    plain       sqlite IntegrityError        postgres IntegrityError

`DEFERRED` and `IMMEDIATE` are indistinguishable on SQLite -- both lose the
constraint -- which is why the rule reads `deferrable=` as present or absent
and does not care which mode was asked for.

`ExclusionConstraint` fails earlier and louder: `manage.py check` returns clean
and `migrate` then stops with `near "EXCLUDE": syntax error`.

Django's own `models.W038` covers the deferrable half, but only when SQLite is
the configured backend, and only as a warning. That overlap is stated in the
rule's limitations rather than hidden.
"""

from __future__ import annotations

import pathlib

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
    """Every DJX-006 finding, having first checked the rule did not crash."""
    result = engine.run(root)
    assert "DJX-006" not in result.rule_errors, result.rule_errors.get("DJX-006")
    return [f for f in result.findings if f.rule_id == "DJX-006"]


def model(constraints: str, imports: str = "") -> str:
    return (
        f"{imports}from django.db import models\n\n\n"
        "class Order(models.Model):\n"
        "    sku = models.CharField(max_length=32)\n\n"
        "    class Meta:\n"
        "        constraints = [\n"
        f"{constraints}"
        "        ]\n"
    )


DEFERRED = model(
    "            models.UniqueConstraint(\n"
    "                fields=['sku'],\n"
    "                name='shop_order_sku',\n"
    "                deferrable=models.Deferrable.DEFERRED,\n"
    "            ),\n"
)

PLAIN = model(
    "            models.UniqueConstraint(fields=['sku'], name='shop_order_sku'),\n",
)


class TestWhenItFires:
    def test_a_deferrable_unique_constraint_is_reported(self, tmp_path: pathlib.Path) -> None:
        found = findings(build(tmp_path, DIVERGENT, DEFERRED))
        assert len(found) == 1
        assert found[0].severity.value == "high"
        assert found[0].confidence.value == "certain"

    def test_it_says_the_constraint_is_dropped_not_the_deferral(
        self, tmp_path: pathlib.Path
    ) -> None:
        # The measured fact and the whole point of the rule. A developer who
        # believes only the deferral is ignored will believe the column is
        # still unique locally, and it is not.
        message = findings(build(tmp_path, DIVERGENT, DEFERRED))[0].message
        assert "drops the constraint" in message
        assert "not enforced there at all" in message

    def test_it_names_the_constraint(self, tmp_path: pathlib.Path) -> None:
        found = findings(build(tmp_path, DIVERGENT, DEFERRED))
        assert "`shop_order_sku`" in found[0].message
        assert found[0].properties["constraint"] == "shop_order_sku"

    def test_it_points_at_the_constraint_not_the_class(self, tmp_path: pathlib.Path) -> None:
        found = findings(build(tmp_path, DIVERGENT, DEFERRED))
        assert found[0].location.line == 9
        assert found[0].location.end_line == 13
        # The evidence has to reach the argument the finding is about, not
        # stop at the opening parenthesis of a five-line call.
        assert "deferrable=models.Deferrable.DEFERRED" in (found[0].location.snippet or "")

    def test_immediate_is_reported_too(self, tmp_path: pathlib.Path) -> None:
        # Measured: IMMEDIATE loses the constraint on SQLite exactly as DEFERRED
        # does. Reading the mode and reporting only one of them would be a rule
        # about Postgres semantics wearing a portability rule's name.
        code = model(
            "            models.UniqueConstraint(\n"
            "                fields=['sku'],\n"
            "                name='shop_order_sku',\n"
            "                deferrable=models.Deferrable.IMMEDIATE,\n"
            "            ),\n"
        )
        assert len(findings(build(tmp_path, DIVERGENT, code))) == 1

    def test_an_exclusion_constraint_is_reported(self, tmp_path: pathlib.Path) -> None:
        code = model(
            "            ExclusionConstraint(name='shop_order_overlap', "
            "expressions=[('window', '&&')]),\n",
            imports="from django.contrib.postgres.constraints import ExclusionConstraint\n",
        )
        found = findings(build(tmp_path, DIVERGENT, code))
        assert len(found) == 1
        assert found[0].properties["kind"] == "ExclusionConstraint"

    def test_the_exclusion_failure_is_described_as_the_louder_one(
        self, tmp_path: pathlib.Path
    ) -> None:
        # A different failure and a different message: nothing is silently
        # unenforced, because nothing gets created at all.
        code = model(
            "            ExclusionConstraint(name='shop_order_overlap', "
            "expressions=[('window', '&&')]),\n",
            imports="from django.contrib.postgres.constraints import ExclusionConstraint\n",
        )
        message = findings(build(tmp_path, DIVERGENT, code))[0].message
        assert "cannot parse `EXCLUDE`" in message
        assert "migrate` stops" in message

    def test_two_offending_constraints_are_two_findings(self, tmp_path: pathlib.Path) -> None:
        code = model(
            "            models.UniqueConstraint(fields=['sku'], name='a', "
            "deferrable=models.Deferrable.DEFERRED),\n"
            "            models.UniqueConstraint(fields=['sku'], name='b', "
            "deferrable=models.Deferrable.IMMEDIATE),\n"
        )
        found = findings(build(tmp_path, DIVERGENT, code))
        assert {f.properties["constraint"] for f in found} == {"a", "b"}

    def test_an_unnamed_constraint_is_still_reported(self, tmp_path: pathlib.Path) -> None:
        # `name` is required by Django, but it may be computed rather than
        # written. Falling back to the class keeps the message readable instead
        # of printing None at the reader.
        code = model(
            "            models.UniqueConstraint(fields=['sku'], name=NAME, "
            "deferrable=models.Deferrable.DEFERRED),\n",
            imports="NAME = 'shop_order_sku'\n",
        )
        found = findings(build(tmp_path, DIVERGENT, code))
        assert len(found) == 1
        assert "a UniqueConstraint on Order" in found[0].message


class TestWhenItStaysSilent:
    def test_a_postgres_only_project_is_not_reported(self, tmp_path: pathlib.Path) -> None:
        assert findings(build(tmp_path, POSTGRES_ONLY, DEFERRED)) == []

    def test_the_divergence_is_what_makes_it_reportable(self, tmp_path: pathlib.Path) -> None:
        assert len(findings(build(tmp_path, DIVERGENT, DEFERRED))) == 1

    def test_a_plain_unique_constraint_is_enforced_on_both(self, tmp_path: pathlib.Path) -> None:
        # Measured: without the argument SQLite emits the UNIQUE clause and
        # rejects the duplicate, exactly as Postgres does.
        assert findings(build(tmp_path, DIVERGENT, PLAIN)) == []

    def test_a_check_constraint_is_enforced_on_both(self, tmp_path: pathlib.Path) -> None:
        code = model(
            "            models.CheckConstraint(condition=models.Q(sku__gt=''), "
            "name='shop_order_sku_set'),\n"
        )
        assert findings(build(tmp_path, DIVERGENT, code)) == []

    def test_deferrable_written_as_none_is_not_a_deferrable_constraint(
        self, tmp_path: pathlib.Path
    ) -> None:
        # The default written out. Reading it as present would report a model
        # that asked for nothing.
        code = model(
            "            models.UniqueConstraint(fields=['sku'], name='shop_order_sku', "
            "deferrable=None),\n"
        )
        assert findings(build(tmp_path, DIVERGENT, code)) == []

    def test_a_project_constraint_that_merely_shares_the_name_is_not_reported(
        self, tmp_path: pathlib.Path
    ) -> None:
        # A project's own ExclusionConstraint is not Django's, and matching on
        # the class name would report someone else's portable constraint.
        code = model(
            "            ExclusionConstraint(name='shop_order_overlap'),\n",
            imports="from shop.custom import ExclusionConstraint\n",
        )
        root = build(tmp_path, DIVERGENT, code)
        (root / "shop" / "custom.py").write_text(
            "from django.db.models import BaseConstraint\n\n\n"
            "class ExclusionConstraint(BaseConstraint):\n    pass\n"
        )
        assert findings(root) == []

    def test_the_package_control_proves_that_silence_is_aimed(self, tmp_path: pathlib.Path) -> None:
        # Presence control for the test above: the same class name, imported
        # from Django's package, is reported.
        code = model(
            "            ExclusionConstraint(name='shop_order_overlap'),\n",
            imports="from django.contrib.postgres.constraints import ExclusionConstraint\n",
        )
        assert len(findings(build(tmp_path, DIVERGENT, code))) == 1

    def test_a_proxy_model_does_not_repeat_its_parent_s_constraint(
        self, tmp_path: pathlib.Path
    ) -> None:
        code = DEFERRED + "\n\nclass OrderProxy(Order):\n    class Meta:\n        proxy = True\n"
        assert len(findings(build(tmp_path, DIVERGENT, code))) == 1


class TestItIsStillTheSameRule:
    def test_it_is_registered_under_the_portability_family(self) -> None:
        from djaudit.models import Family
        from djaudit.registry import get

        assert get("DJX-006").meta.family is Family.DJX

    def test_it_admits_django_already_warns_about_half_of_it(self) -> None:
        # W038 exists and the rule says so. A rule that quietly re-reports what
        # the framework already told you, without saying it is doing that, is
        # how a tool loses the reader's trust on everything else it says.
        from djaudit.registry import get

        limits = get("DJX-006").meta.limitations
        assert any("models.W038" in limit for limit in limits)
        assert any("does not fail `manage.py check`" in limit for limit in limits)

    def test_it_records_that_nothing_warns_about_exclusion_constraints(self) -> None:
        from djaudit.registry import get

        assert any(
            "Nothing warns about `ExclusionConstraint`" in limit
            for limit in get("DJX-006").meta.limitations
        )

    def test_the_remediation_asks_whether_the_deferral_is_needed(self) -> None:
        from djaudit.registry import get

        assert "actually needed" in get("DJX-006").meta.remediation


class TestWhatItSays:
    """Mutation found the message template and the config citation unread.

    Substring assertions elsewhere in this file each check one clause and
    leave the joins between them unchecked, so a mutant that blanked ``" is a
    "`` survived every one of them. These read the whole string.
    """

    def test_the_deferrable_message_reads_as_one_sentence(self, tmp_path: pathlib.Path) -> None:
        assert findings(build(tmp_path, DIVERGENT, DEFERRED))[0].message == (
            "`shop_order_sku` on Order is a UniqueConstraint that SQLite does not "
            "enforce -- SQLite drops the constraint from `CREATE TABLE` rather than "
            "declining the deferral, so the uniqueness is not enforced there at all "
            "and two rows sharing the value are accepted in development and rejected "
            "in production"
        )

    def test_the_exclusion_message_reads_as_one_sentence(self, tmp_path: pathlib.Path) -> None:
        code = model(
            "            ExclusionConstraint(name='shop_order_overlap', "
            "expressions=[('window', '&&')]),\n",
            imports="from django.contrib.postgres.constraints import ExclusionConstraint\n",
        )
        assert findings(build(tmp_path, DIVERGENT, code))[0].message == (
            "`shop_order_overlap` on Order is a ExclusionConstraint that SQLite does "
            "not enforce -- SQLite cannot parse `EXCLUDE` and `migrate` stops there "
            "with a syntax error, so the whole schema is unreachable on that engine"
        )

    def test_the_deferrable_finding_cites_the_feature_flag(self, tmp_path: pathlib.Path) -> None:
        # Read out of Django rather than remembered:
        #   sqlite3 supports_deferrable_unique_constraints = False
        #   postgresql                                     = True
        config = [
            e
            for e in findings(build(tmp_path, DIVERGENT, DEFERRED))[0].evidence
            if e.kind.value == "config"
        ]
        assert len(config) == 1
        assert config[0].content == (
            "django.db.backends.sqlite3: supports_deferrable_unique_constraints = "
            "False (postgresql: True)"
        )
        assert config[0].source == "measured against django 6.0 and sqlite 3"

    def test_the_exclusion_finding_cites_the_measured_failure_instead(
        self, tmp_path: pathlib.Path
    ) -> None:
        # There is no feature flag for this one. Citing the deferrable flag
        # here would attach a true statement to a finding it does not explain.
        code = model(
            "            ExclusionConstraint(name='shop_order_overlap', "
            "expressions=[('window', '&&')]),\n",
            imports="from django.contrib.postgres.constraints import ExclusionConstraint\n",
        )
        config = [
            e
            for e in findings(build(tmp_path, DIVERGENT, code))[0].evidence
            if e.kind.value == "config"
        ]
        assert config[0].content == (
            'sqlite3.OperationalError: near "EXCLUDE": syntax error -- raised by '
            "`migrate`; `manage.py check` reports no issues beforehand"
        )
        assert "supports_deferrable" not in config[0].content

    def test_the_source_evidence_quotes_the_constraint_from_the_model_file(
        self, tmp_path: pathlib.Path
    ) -> None:
        found = findings(build(tmp_path, DIVERGENT, DEFERRED))[0]
        source = [e for e in found.evidence if e.kind.value == "source"]
        assert len(source) == 1
        assert source[0].source == "shop/models.py"
        assert "deferrable=models.Deferrable.DEFERRED" in source[0].content

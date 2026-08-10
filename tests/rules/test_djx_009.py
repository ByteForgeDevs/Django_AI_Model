"""DJX-009 -- column limits SQLite declares and then ignores.

The plan called this "`max_length` enforced by Postgres but not SQLite", and
measuring it found the premise true and too narrow. SQLite has type *affinity*
where Postgres has type *constraints*, so the divergence is not about one
keyword on one field class. Measured on a real SQLite and the live Postgres,
writing through `create()`, `save()`, `bulk_create()` and `update()`:

    CharField(max_length=10)  <- "x"*50   sqlite stores, reads back 50 chars
                                          postgres DataError: value too long
                                                   for type character varying(10)
    IntegerField              <- 2**31    sqlite stores 2147483648
                                          postgres DataError: integer out of range
    SmallIntegerField         <- 2**15    postgres DataError: smallint out of range
    DecimalField(max_digits=4) <- 12345.67 sqlite stores 12345.67
                                          postgres DataError: numeric field overflow
    SlugField / EmailField / URLField     varchar(50) / (254) / (200), all the same

Three shapes were measured to *agree* and are deliberately excluded, and each
one would have been a false positive:

    TextField(max_length=10)  <- "x"*50   accepted by BOTH -- that argument is a
                                          form validator, not a column constraint
    BigIntegerField           <- 2**62    accepted by both
    PositiveIntegerField      <- -1       IntegrityError on both; Django emits the
                                          `>= 0` CHECK on each engine

The `Positive` variants are in the range table anyway, because the *upper*
bound does diverge: 2**31 into a `PositiveIntegerField` is stored by SQLite and
raises `integer out of range` on Postgres. Sign agrees, range does not.

`full_clean()` raises `ValidationError` identically on both, which is why the
remediation can recommend it: none of the four write paths calls it.
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


def build(tmp_path: pathlib.Path, databases: str, fields: str) -> pathlib.Path:
    root = tmp_path / f"proj{len(list(tmp_path.iterdir()))}"
    (root / "config").mkdir(parents=True)
    (root / "shop").mkdir()
    (root / "manage.py").write_text(
        "import os\nos.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')\n"
    )
    (root / "config" / "__init__.py").write_text("")
    (root / "config" / "settings.py").write_text(MARKERS + databases)
    (root / "shop" / "__init__.py").write_text("")
    (root / "shop" / "models.py").write_text(
        "from django.db import models\n\n\nclass Order(models.Model):\n" + fields
    )
    return root


def findings(root: pathlib.Path) -> list[Finding]:
    result = engine.run(root)
    assert "DJX-009" not in result.rule_errors, result.rule_errors.get("DJX-009")
    return [f for f in result.findings if f.rule_id == "DJX-009"]


def only(root: pathlib.Path) -> Finding:
    found = findings(root)
    assert len(found) == 1, found
    return found[0]


class TestTheColumnsItCounts:
    def test_a_charfield_is_counted_with_its_declared_width(self, tmp_path: pathlib.Path) -> None:
        found = only(build(tmp_path, DIVERGENT, "    sku = models.CharField(max_length=32)\n"))
        assert found.properties["text"] == "1"
        assert "Order.sku (shop/models.py:5) is varchar(32)" in found.evidence[0].content

    def test_the_implicit_widths_are_known(self, tmp_path: pathlib.Path) -> None:
        # Django supplies these, so a reader of the source never sees a number
        # and neither would a rule that only looked for `max_length=`.
        found = only(
            build(
                tmp_path,
                DIVERGENT,
                "    slug = models.SlugField()\n"
                "    email = models.EmailField()\n"
                "    site = models.URLField()\n",
            )
        )
        assert found.properties["text"] == "3"
        content = found.evidence[0].content
        assert "is varchar(50)" in content
        assert "is varchar(254)" in content
        assert "is varchar(200)" in content

    def test_the_integer_widths_are_counted(self, tmp_path: pathlib.Path) -> None:
        found = only(
            build(
                tmp_path,
                DIVERGENT,
                "    qty = models.IntegerField()\n    tiny = models.SmallIntegerField()\n",
            )
        )
        assert found.properties["integer"] == "2"
        assert "Order.qty (shop/models.py:5) is integer" in found.evidence[0].content
        assert "Order.tiny (shop/models.py:6) is smallint" in found.evidence[0].content

    def test_the_positive_variants_are_counted_for_their_range(
        self, tmp_path: pathlib.Path
    ) -> None:
        # Measured: 2**31 into a PositiveIntegerField is stored by SQLite and
        # raises `integer out of range` on Postgres. The `>= 0` check agrees on
        # both engines, so this entry is about the ceiling and not the sign.
        found = only(
            build(
                tmp_path,
                DIVERGENT,
                "    qty = models.PositiveIntegerField()\n"
                "    tiny = models.PositiveSmallIntegerField()\n",
            )
        )
        assert found.properties["integer"] == "2"
        assert "is integer" in found.evidence[0].content
        assert "is smallint" in found.evidence[0].content

    def test_a_decimal_field_is_counted(self, tmp_path: pathlib.Path) -> None:
        found = only(
            build(
                tmp_path,
                DIVERGENT,
                "    price = models.DecimalField(max_digits=4, decimal_places=2)\n",
            )
        )
        assert found.properties["decimal"] == "1"

    def test_fields_on_every_model_are_counted(self, tmp_path: pathlib.Path) -> None:
        root = build(tmp_path, DIVERGENT, "    sku = models.CharField(max_length=32)\n")
        (root / "shop" / "models.py").write_text(
            "from django.db import models\n\n\n"
            "class Order(models.Model):\n    sku = models.CharField(max_length=32)\n\n\n"
            "class Invoice(models.Model):\n    ref = models.CharField(max_length=8)\n"
        )
        assert only(root).properties["text"] == "2"


class TestWhatItLeavesAlone:
    """Each of these was measured to agree, and each would be a false positive."""

    def test_a_textfield_with_a_max_length_is_not_counted(self, tmp_path: pathlib.Path) -> None:
        # The trap. It reads exactly like CharField's and produces no column
        # constraint at all: a fifty-character value into TextField(max_length=10)
        # was accepted by both engines.
        assert (
            findings(build(tmp_path, DIVERGENT, "    body = models.TextField(max_length=10)\n"))
            == []
        )

    def test_a_plain_textfield_is_not_counted(self, tmp_path: pathlib.Path) -> None:
        assert findings(build(tmp_path, DIVERGENT, "    body = models.TextField()\n")) == []

    def test_a_big_integer_field_is_not_counted(self, tmp_path: pathlib.Path) -> None:
        # bigint holds everything SQLite will; measured to agree at 2**62.
        assert findings(build(tmp_path, DIVERGENT, "    n = models.BigIntegerField()\n")) == []

    def test_the_exclusions_are_not_simply_a_dead_rule(self, tmp_path: pathlib.Path) -> None:
        # Presence control for the three tests above: the same model with one
        # CharField added is reported, so their silence is about the field
        # types and not about the project being unreadable.
        found = only(
            build(
                tmp_path,
                DIVERGENT,
                "    body = models.TextField(max_length=10)\n"
                "    n = models.BigIntegerField()\n"
                "    sku = models.CharField(max_length=32)\n",
            )
        )
        assert found.properties["text"] == "1"
        assert found.properties["integer"] == "0"

    def test_an_unreadable_width_is_skipped_rather_than_guessed(
        self, tmp_path: pathlib.Path
    ) -> None:
        root = build(tmp_path, DIVERGENT, "    sku = models.CharField(max_length=SKU_LEN)\n")
        (root / "shop" / "models.py").write_text(
            "from django.db import models\n\n"
            "from shop.conf import SKU_LEN\n\n\n"
            "class Order(models.Model):\n    sku = models.CharField(max_length=SKU_LEN)\n"
        )
        (root / "shop" / "conf.py").write_text("SKU_LEN = 32\n")
        assert findings(root) == []

    def test_a_readable_width_beside_it_is_still_counted(self, tmp_path: pathlib.Path) -> None:
        # Presence control: skipping the unreadable field must not abandon the
        # model, which is the failure mode that makes a count untrustworthy.
        root = build(tmp_path, DIVERGENT, "    sku = models.CharField(max_length=32)\n")
        (root / "shop" / "models.py").write_text(
            "from django.db import models\n\n"
            "from shop.conf import SKU_LEN\n\n\n"
            "class Order(models.Model):\n"
            "    sku = models.CharField(max_length=SKU_LEN)\n"
            "    name = models.CharField(max_length=200)\n"
        )
        (root / "shop" / "conf.py").write_text("SKU_LEN = 32\n")
        assert only(root).properties["text"] == "1"

    def test_an_implicit_width_overridden_unreadably_is_skipped(
        self, tmp_path: pathlib.Path
    ) -> None:
        # The subtle one. A SlugField has a known default width of 50, so a
        # rule that reached for the default would report varchar(50) for a
        # column that is actually varchar(N) -- a confident wrong number,
        # which is worse than silence.
        root = build(tmp_path, DIVERGENT, "    s = models.SlugField()\n")
        (root / "shop" / "models.py").write_text(
            "from django.db import models\n\n"
            "from shop.conf import N\n\n\n"
            "class Order(models.Model):\n    s = models.SlugField(max_length=N)\n"
        )
        (root / "shop" / "conf.py").write_text("N = 120\n")
        assert findings(root) == []

    def test_a_readable_slug_is_counted(self, tmp_path: pathlib.Path) -> None:
        # Presence control for the test above.
        found = only(build(tmp_path, DIVERGENT, "    s = models.SlugField()\n"))
        assert "is varchar(50)" in found.evidence[0].content

    def test_a_decimal_field_with_no_readable_arguments_is_skipped(
        self, tmp_path: pathlib.Path
    ) -> None:
        root = build(tmp_path, DIVERGENT, "    p = models.DecimalField(max_digits=4)\n")
        (root / "shop" / "models.py").write_text(
            "from django.db import models\n\n"
            "MONEY = {'max_digits': 9, 'decimal_places': 2}\n\n\n"
            "class Order(models.Model):\n    p = models.DecimalField(**MONEY)\n"
        )
        assert findings(root) == []

    def test_a_postgres_only_project_is_not_reported(self, tmp_path: pathlib.Path) -> None:
        assert (
            findings(build(tmp_path, POSTGRES_ONLY, "    sku = models.CharField(max_length=32)\n"))
            == []
        )

    def test_a_project_with_no_such_column_is_not_reported(self, tmp_path: pathlib.Path) -> None:
        # The gate that stops this being a restatement of DJX-001: running two
        # engines is not by itself a reason to emit this finding.
        assert findings(build(tmp_path, DIVERGENT, "    body = models.TextField()\n")) == []


class TestItSaysItOnce:
    def test_eight_columns_are_one_finding(self, tmp_path: pathlib.Path) -> None:
        # Per-field would be one finding per CharField in the codebase. The
        # count carries the same information and cannot drown anything.
        fields = "".join(f"    f{i} = models.CharField(max_length=32)\n" for i in range(8))
        assert only(build(tmp_path, DIVERGENT, fields)).properties["text"] == "8"

    def test_the_sample_is_capped_and_says_so(self, tmp_path: pathlib.Path) -> None:
        fields = "".join(f"    f{i} = models.CharField(max_length=32)\n" for i in range(8))
        evidence = only(build(tmp_path, DIVERGENT, fields)).evidence[0]
        assert len(evidence.content.splitlines()) == 6
        assert evidence.source == "8 columns; first 6 shown"

    def test_a_short_sample_is_not_described_as_capped(self, tmp_path: pathlib.Path) -> None:
        evidence = only(
            build(tmp_path, DIVERGENT, "    sku = models.CharField(max_length=32)\n")
        ).evidence[0]
        assert evidence.source == "1 columns"

    def test_it_is_reported_against_the_settings_that_diverge(self, tmp_path: pathlib.Path) -> None:
        # The columns are evidence; the decision a reader can act on is the
        # engine pair, so the finding points where that is written.
        found = only(build(tmp_path, DIVERGENT, "    sku = models.CharField(max_length=32)\n"))
        assert found.location.file == "config/settings.py"


class TestTheSettingsItPointsAt:
    def test_an_if_else_with_no_default_engine_still_reports(self, tmp_path: pathlib.Path) -> None:
        # Both branches opt-in, so no engine is the one a developer gets by
        # doing nothing and there is no settings statement to point at. This
        # shape is common -- an if/else on an env var -- so the fallback is
        # live code rather than defensive padding.
        both_conditional = (
            "import os\n"
            "if os.getenv('DB') == 'pg':\n"
            f"    DATABASES = {{'default': {{'ENGINE': '{PG}'}}}}\n"
            "else:\n"
            f"    DATABASES = {{'default': {{'ENGINE': '{LITE}'}}}}\n"
        )
        found = only(
            build(tmp_path, both_conditional, "    sku = models.CharField(max_length=32)\n")
        )
        assert found.location.file == "manage.py"
        assert found.location.line == 1

    def test_it_points_at_the_alias_rather_than_the_whole_assignment(
        self, tmp_path: pathlib.Path
    ) -> None:
        # `DIVERGENT` writes the dict on one line, which makes the alias node
        # and the `DATABASES = ...` statement share a line number and hides
        # which of the two is used. Spread over several lines they differ:
        # measured, the alias dict is line 6 and the assignment is line 5.
        multiline = (
            "DATABASES = {\n"
            "    'default': {\n"
            f"        'ENGINE': '{LITE}',\n"
            "    },\n"
            "}\n"
            "import os\n"
            "if os.getenv('DB') == 'pg':\n"
            f"    DATABASES = {{'default': {{'ENGINE': '{PG}'}}}}\n"
        )
        found = only(build(tmp_path, multiline, "    sku = models.CharField(max_length=32)\n"))
        assert found.location.file == "config/settings.py"
        assert found.location.line == 6


class TestWhatItSays:
    def test_one_column_is_said_in_the_singular(self, tmp_path: pathlib.Path) -> None:
        assert only(
            build(tmp_path, DIVERGENT, "    sku = models.CharField(max_length=32)\n")
        ).message == (
            "1 text column declares a width that SQLite will not enforce, so this "
            "column accepts values in development that Postgres rejects with "
            "DataError in production"
        )

    def test_all_three_kinds_are_listed_together(self, tmp_path: pathlib.Path) -> None:
        assert only(
            build(
                tmp_path,
                DIVERGENT,
                "    sku = models.CharField(max_length=32)\n"
                "    qty = models.IntegerField()\n"
                "    price = models.DecimalField(max_digits=4, decimal_places=2)\n",
            )
        ).message == (
            "1 text column declares a width, 1 integer column declares a range and "
            "1 decimal column declares a precision that SQLite will not enforce, so "
            "these columns accept values in development that Postgres rejects with "
            "DataError in production"
        )

    def test_many_columns_are_said_in_the_plural(self, tmp_path: pathlib.Path) -> None:
        # Every other message assertion here uses one column, which left the
        # plural noun and the plural verb free to be anything at all. All three
        # kinds are pluralised at once because each carries its own noun pair,
        # and a text-only plural leaves the other two unwitnessed.
        fields = (
            "".join(f"    f{i} = models.CharField(max_length=32)\n" for i in range(3))
            + "".join(f"    n{i} = models.IntegerField()\n" for i in range(2))
            + "".join(
                f"    d{i} = models.DecimalField(max_digits=4, decimal_places=2)\n"
                for i in range(2)
            )
        )
        assert only(build(tmp_path, DIVERGENT, fields)).message == (
            "3 text columns declare a width, 2 integer columns declare a range and "
            "2 decimal columns declare a precision that SQLite will not enforce, so "
            "these columns accept values in development that Postgres rejects with "
            "DataError in production"
        )

    def test_a_kind_with_no_columns_is_not_mentioned(self, tmp_path: pathlib.Path) -> None:
        message = only(build(tmp_path, DIVERGENT, "    qty = models.IntegerField()\n")).message
        assert "integer column" in message
        assert "text column" not in message
        assert "decimal" not in message

    def test_the_evidence_shows_the_measurement(self, tmp_path: pathlib.Path) -> None:
        config = [
            e
            for e in only(
                build(tmp_path, DIVERGENT, "    sku = models.CharField(max_length=32)\n")
            ).evidence
            if e.kind.value == "config"
        ]
        assert config[0].content == (
            'sqlite:   varchar(10) <- "x"*50 stored, reads back 50 chars\n'
            "postgres: DataError: value too long for type character varying(10)\n"
            "both:     full_clean() raises ValidationError before either"
        )
        assert config[0].source == "measured against django 6.0"


class TestItIsStillTheSameRule:
    def test_it_is_registered_under_the_portability_family(self) -> None:
        from djaudit.models import Family
        from djaudit.registry import get

        assert get("DJX-009").meta.family is Family.DJX

    def test_the_rationale_says_the_value_is_kept_not_truncated(self) -> None:
        # Measured: SQLite hands back all fifty characters. "Truncates" is the
        # intuitive guess and it is wrong, which is worth stating explicitly
        # because a reader who believes it will look for the wrong symptom.
        from djaudit.registry import get

        assert "it is not truncated, it is simply kept" in get("DJX-009").meta.rationale

    def test_the_rationale_names_the_four_write_paths(self) -> None:
        from djaudit.registry import get

        rationale = get("DJX-009").meta.rationale
        for path in ("create()", "save()", "bulk_create()", "update()"):
            assert path in rationale

    def test_the_remediation_offers_something_that_works_on_both(self) -> None:
        from djaudit.registry import get

        assert "full_clean()" in get("DJX-009").meta.remediation

    def test_it_admits_it_does_not_prove_anything_writes_an_overlong_value(
        self,
    ) -> None:
        from djaudit.registry import get

        assert any(
            "not that any code actually writes an over-long value" in limit
            for limit in get("DJX-009").meta.limitations
        )

    def test_it_says_textfield_was_excluded_by_measurement(self) -> None:
        from djaudit.registry import get

        assert any(
            "excluded on measurement rather than on principle" in limit
            for limit in get("DJX-009").meta.limitations
        )

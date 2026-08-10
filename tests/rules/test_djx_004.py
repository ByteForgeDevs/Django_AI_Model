"""DJX-004 -- case-sensitive text lookups in a project that also runs SQLite.

The quietest rule in the family and the reason the family exists. `DJX-002` and
`DJX-003` both end in an exception; this one ends in a different answer. The
query runs on both backends, returns rows on both backends, and returns
different rows, and nothing anywhere says so.

Measured on a real SQLite and the live Postgres before it was written, against
a row holding 'Hello':

    text__contains='hello'      sqlite 1   postgres 0
    text__startswith='hello'    sqlite 1   postgres 0
    text__endswith='LLO'        sqlite 1   postgres 0
    text__exact='hello'         sqlite 0   postgres 0
    text__regex='hello'         sqlite 0   postgres 0

`exact` and `regex` are in that table because they were candidates and the
measurement removed them. The folding is also ASCII-only: against a row holding
'ÉCOLE', `contains='école'` matched on neither.
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

MODELS = (
    "from django.db import models\n\n\n"
    "class Order(models.Model):\n"
    "    sku = models.CharField(max_length=32)\n"
    "    body = models.TextField()\n"
    "    slug = models.SlugField()\n"
    "    email = models.EmailField()\n"
    "    homepage = models.URLField()\n"
    "    archive = models.FilePathField(path='/srv')\n"
    "    quantity = models.IntegerField()\n"
    "    data = models.JSONField(default=dict)\n"
)


def build(tmp_path: pathlib.Path, databases: str, code: str, models: str = MODELS) -> pathlib.Path:
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
    (root / "shop" / "queries.py").write_text(code)
    return root


def findings(root: pathlib.Path) -> list[Finding]:
    """Every DJX-004 finding, having first checked the rule did not crash."""
    result = engine.run(root)
    assert "DJX-004" not in result.rule_errors, result.rule_errors.get("DJX-004")
    return [f for f in result.findings if f.rule_id == "DJX-004"]


def query(body: str) -> str:
    return f"from shop.models import Order\n\n\ndef go(value):\n    return {body}\n"


CONTAINS = query("Order.objects.filter(sku__contains=value)")


class TestWhenItFires:
    def test_contains_on_a_char_field_is_reported(self, tmp_path: pathlib.Path) -> None:
        found = findings(build(tmp_path, DIVERGENT, CONTAINS))
        assert len(found) == 1
        assert found[0].severity.value == "medium"
        assert found[0].confidence.value == "firm"

    def test_the_message_says_which_way_the_difference_runs(self, tmp_path: pathlib.Path) -> None:
        # Which direction matters: the developer sees more rows, not fewer, so
        # the failure mode is a feature that quietly stops working in
        # production rather than one that visibly never worked.
        message = findings(build(tmp_path, DIVERGENT, CONTAINS))[0].message
        assert "'sku__contains'" in message
        assert "more rows in development" in message

    def test_django_s_own_feature_flag_is_the_evidence(self, tmp_path: pathlib.Path) -> None:
        found = findings(build(tmp_path, DIVERGENT, CONTAINS))
        flags = [e for e in found[0].evidence if e.source == "django feature flags"]
        assert len(flags) == 1
        assert "has_case_insensitive_like = True" in flags[0].content

    def test_startswith_is_the_same_difference(self, tmp_path: pathlib.Path) -> None:
        code = query("Order.objects.filter(sku__startswith=value)")
        assert len(findings(build(tmp_path, DIVERGENT, code))) == 1

    def test_endswith_is_the_same_difference(self, tmp_path: pathlib.Path) -> None:
        code = query("Order.objects.filter(sku__endswith=value)")
        assert len(findings(build(tmp_path, DIVERGENT, code))) == 1

    def test_a_text_field_is_a_string_column_too(self, tmp_path: pathlib.Path) -> None:
        code = query("Order.objects.filter(body__contains=value)")
        assert len(findings(build(tmp_path, DIVERGENT, code))) == 1

    def test_the_narrower_string_fields_are_string_columns_too(
        self, tmp_path: pathlib.Path
    ) -> None:
        code = query("Order.objects.filter(slug__contains=value, email__endswith=value)")
        found = findings(build(tmp_path, DIVERGENT, code))
        assert len(found) == 1
        assert found[0].properties["fields"] == "email slug"

    def test_a_url_field_is_a_string_column_too(self, tmp_path: pathlib.Path) -> None:
        # URLField is a CharField with a validator attached; the column is
        # still varchar and LIKE still folds.
        code = query("Order.objects.filter(homepage__startswith=value)")
        assert len(findings(build(tmp_path, DIVERGENT, code))) == 1

    def test_a_file_path_field_is_a_string_column_too(self, tmp_path: pathlib.Path) -> None:
        # The one people forget, and the one where the divergence bites: paths
        # are case-sensitive on the filesystems these projects deploy to, so a
        # SQLite-only match finds a file that production will not open.
        code = query("Order.objects.filter(archive__contains=value)")
        assert len(findings(build(tmp_path, DIVERGENT, code))) == 1

    def test_two_lookups_in_one_call_are_one_finding_that_names_both(
        self, tmp_path: pathlib.Path
    ) -> None:
        # One call is one place to fix, so it is one finding -- but the message
        # has to name both, or the developer fixes the first and leaves the
        # second behind believing the finding is closed.
        code = query("Order.objects.filter(slug__contains=value, email__endswith=value)")
        found = findings(build(tmp_path, DIVERGENT, code))
        assert len(found) == 1
        assert "'slug__contains' and 'email__endswith'" in found[0].message
        assert found[0].properties["lookups"] == "slug__contains email__endswith"

    def test_a_double_star_beside_the_lookup_does_not_stop_it(self, tmp_path: pathlib.Path) -> None:
        # `**extra` has no keyword name at all. Reading one off it is an
        # attribute error on None, which the rule swallows into a rule error
        # and reports nothing -- so the finding here is the evidence it does not.
        code = query("Order.objects.filter(sku__contains=value, **extra)")
        assert len(findings(build(tmp_path, DIVERGENT, code))) == 1

    def test_an_ordinary_keyword_beside_the_lookup_is_skipped_not_read(
        self, tmp_path: pathlib.Path
    ) -> None:
        # `status='open'` is a keyword with a name that ends in none of the
        # three suffixes. It has to be stepped over, not searched for one.
        code = query("Order.objects.filter(sku__contains=value, quantity=1)")
        found = findings(build(tmp_path, DIVERGENT, code))
        assert len(found) == 1
        assert found[0].properties["lookups"] == "sku__contains"

    def test_a_keyword_expansion_elsewhere_in_the_file_does_not_break_it(
        self, tmp_path: pathlib.Path
    ) -> None:
        # The prefilter means a file holding only `filter(**criteria)` is never
        # parsed, so the nameless-keyword guards can only be reached by a file
        # that also contains one of the three words. This is that file.
        code = (
            "from shop.models import Order\n\n\n"
            "def search(criteria):\n"
            "    return Order.objects.filter(**criteria)\n\n\n"
            "def go(value):\n"
            "    return Order.objects.filter(sku__contains=value)\n"
        )
        assert len(findings(build(tmp_path, DIVERGENT, code))) == 1


class TestWhenItStaysSilent:
    def test_a_postgres_only_project_is_not_reported(self, tmp_path: pathlib.Path) -> None:
        assert findings(build(tmp_path, POSTGRES_ONLY, CONTAINS)) == []

    def test_the_divergence_is_what_makes_it_reportable(self, tmp_path: pathlib.Path) -> None:
        assert len(findings(build(tmp_path, DIVERGENT, CONTAINS))) == 1

    def test_the_case_insensitive_spelling_is_the_remediation_not_the_defect(
        self, tmp_path: pathlib.Path
    ) -> None:
        # `icontains` asks for case-insensitivity out loud and gets it on both
        # backends. Reporting it would be complaining about the fix.
        code = query(
            "Order.objects.filter("
            "sku__icontains=value, body__istartswith=value, slug__iendswith=value)"
        )
        assert findings(build(tmp_path, DIVERGENT, code)) == []

    def test_exact_does_not_differ_and_is_not_reported(self, tmp_path: pathlib.Path) -> None:
        # Measured on both engines: `text__exact='hello'` returns nothing
        # against a row holding 'Hello' on either. `=` is not LIKE.
        assert findings(build(tmp_path, DIVERGENT, query("Order.objects.filter(sku=value)"))) == []

    def test_regex_does_not_differ_and_is_not_reported(self, tmp_path: pathlib.Path) -> None:
        # Also measured: Django implements REGEXP for SQLite in Python and does
        # not fold case, so both engines agree.
        code = query("Order.objects.filter(sku__regex=value)")
        assert findings(build(tmp_path, DIVERGENT, code)) == []

    def test_contains_on_a_json_field_is_a_different_rule(self, tmp_path: pathlib.Path) -> None:
        # `data__contains` is containment, not a substring match, and SQLite
        # raises rather than answering differently. Reporting it here as well
        # would give one query two findings that contradict each other.
        found = findings(
            build(tmp_path, DIVERGENT, query("Order.objects.filter(data__contains=value)"))
        )
        assert found == []

    def test_the_json_field_control_proves_that_silence_is_aimed(
        self, tmp_path: pathlib.Path
    ) -> None:
        # The presence control for the test above: the same keyword shape on a
        # string column is reported, so the silence is about the field type.
        assert len(findings(build(tmp_path, DIVERGENT, CONTAINS))) == 1

    def test_a_non_text_column_is_not_reported(self, tmp_path: pathlib.Path) -> None:
        code = query("Order.objects.filter(quantity__contains=value)")
        assert findings(build(tmp_path, DIVERGENT, code)) == []

    def test_a_custom_field_is_not_reported(self, tmp_path: pathlib.Path) -> None:
        # A subclass may override `get_prep_value`, `db_type` or the lookup
        # itself. We cannot know what it changed, so we do not speak about it.
        models = (
            "from django.db import models\n\n\n"
            "class Secret(models.CharField):\n    pass\n\n\n"
            "class Order(models.Model):\n    sku = Secret(max_length=32)\n"
        )
        assert findings(build(tmp_path, DIVERGENT, CONTAINS, models)) == []

    def test_an_annotation_keyword_is_not_a_lookup(self, tmp_path: pathlib.Path) -> None:
        # `annotate`'s keywords name the annotation being created, not a column
        # being filtered, so reading them as lookups is how a rule invents a
        # field. The tracker does resolve this call to `shop.Order` -- measured
        # -- so the silence is the method check and not a failure to follow it.
        code = query("Order.objects.annotate(sku__contains=value)")
        assert findings(build(tmp_path, DIVERGENT, code)) == []

    def test_a_project_s_own_queryset_method_is_not_a_lookup(self, tmp_path: pathlib.Path) -> None:
        # A custom `search(**kwargs)` on a queryset may forward its keywords to
        # `filter`, or may not; it may lower the column first, or hand the term
        # to a search backend. Only Django's own five lookup methods have a
        # documented meaning for these keywords, so only those are read.
        code = query("Order.objects.search(sku__contains=value)")
        assert findings(build(tmp_path, DIVERGENT, code)) == []

    def test_a_transform_before_the_lookup_is_not_reported(self, tmp_path: pathlib.Path) -> None:
        # `sku__lower__contains` needs `CharField.register_lookup(Lower)` to be
        # legal, and then it means `LOWER(sku) LIKE ...`, which is a different
        # query from the one this rule describes. The path does not resolve to
        # a field, and the rule does not shorten it back until one appears --
        # doing that would attribute the lookup to whatever column happens to
        # share its first segment.
        code = query("Order.objects.filter(sku__lower__contains=value)")
        assert findings(build(tmp_path, DIVERGENT, code)) == []

    def test_the_unshortened_path_is_what_makes_it_silent(self, tmp_path: pathlib.Path) -> None:
        # The presence control for the test above: the same column, the same
        # suffix, one segment fewer, and it is reported.
        assert len(findings(build(tmp_path, DIVERGENT, CONTAINS))) == 1

    def test_a_queryset_rebound_in_a_loop_is_not_reported(self, tmp_path: pathlib.Path) -> None:
        # A false negative, and a deliberate one: `q = q.filter(...)` inside a
        # loop gives the name two definitions and the tracker declines to pick
        # between them. Healthchecks writes exactly this shape at
        # `hc/api/views.py:423` and it is not reported, while the same lookup at
        # `hc/api/views.py:750` is. Guessing instead would cost precision across
        # every rule built on the tracker, not just this one.
        code = (
            "from shop.models import Order\n\n\n"
            "def go(values):\n"
            "    q = Order.objects.all()\n"
            "    for value in values:\n"
            "        q = q.filter(sku__contains=value)\n"
            "    return q\n"
        )
        assert findings(build(tmp_path, DIVERGENT, code)) == []

    def test_a_queryset_rebound_twice_is_not_reported(self, tmp_path: pathlib.Path) -> None:
        # The same limitation reached the other way: a conditional rebinding
        # before the lookup leaves two definitions live.
        code = (
            "from shop.models import Order\n\n\n"
            "def go(value, flag):\n"
            "    q = Order.objects.all()\n"
            "    if flag:\n"
            "        q = q.only('sku')\n"
            "    q = q.filter(sku__contains=value)\n"
            "    return q\n"
        )
        assert findings(build(tmp_path, DIVERGENT, code)) == []

    def test_one_rebinding_is_still_followed(self, tmp_path: pathlib.Path) -> None:
        # The presence control for both tests above. Without it they would pass
        # for a rule that had simply stopped following assignments at all.
        code = (
            "from shop.models import Order\n\n\n"
            "def go(value):\n"
            "    q = Order.objects.all()\n"
            "    q = q.filter(sku__contains=value)\n"
            "    return q\n"
        )
        assert len(findings(build(tmp_path, DIVERGENT, code))) == 1


class TestItIsStillTheSameRule:
    def test_it_is_registered_under_the_portability_family(self) -> None:
        from djaudit.models import Family
        from djaudit.registry import get

        assert get("DJX-004").meta.family is Family.DJX

    def test_it_admits_the_difference_depends_on_the_data(self) -> None:
        from djaudit.registry import get

        assert any("depends on the data" in limit for limit in get("DJX-004").meta.limitations)

    def test_it_records_that_the_folding_is_ascii_only(self) -> None:
        from djaudit.registry import get

        assert any("ASCII-only" in limit for limit in get("DJX-004").meta.limitations)

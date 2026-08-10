"""DJX-002 -- JSON containment in a project that also runs SQLite.

The rule that shows why `QueryRule` is given a model. `name__contains` and
`data__contains` are the same nine characters and two different questions: one
is a substring match every database performs, the other is a containment test
SQLite has no operator for and Django refuses to approximate. Nothing in the
keyword distinguishes them, so a rule that reads only the source text is either
silent or wrong, and which one it is depends on the project.

Measured against a real SQLite and a real Postgres before it was written:
`data__contains={'n': 1}` raises `NotSupportedError` on SQLite and returns the
row on Postgres, and so does `data__contained_by`. `data__has_key('n')` returns
the row on both, which is what makes the remediation an actual alternative
rather than an instruction to stop asking.
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
    "class Supplier(models.Model):\n"
    "    name = models.CharField(max_length=50)\n"
    "    profile = models.JSONField(default=dict)\n\n\n"
    "class Order(models.Model):\n"
    "    sku = models.CharField(max_length=32)\n"
    "    notes = models.CharField(max_length=200)\n"
    "    data = models.JSONField(default=dict)\n"
    "    supplier = models.ForeignKey(Supplier, on_delete=models.CASCADE)\n"
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
    """Every DJX-002 finding, having first checked the rule did not crash.

    The engine records a rule's exceptions rather than propagating them, so an
    unguarded `== []` assertion passes for a rule that never ran at all.
    """
    result = engine.run(root)
    assert "DJX-002" not in result.rule_errors, result.rule_errors.get("DJX-002")
    return [f for f in result.findings if f.rule_id == "DJX-002"]


def query(body: str) -> str:
    return f"from shop.models import Order\n\n\ndef go(value):\n    return {body}\n"


CONTAINS = query("Order.objects.filter(data__contains=value)")


class TestWhenItFires:
    def test_containment_on_a_json_field_is_reported(self, tmp_path: pathlib.Path) -> None:
        found = findings(build(tmp_path, DIVERGENT, CONTAINS))
        assert len(found) == 1
        assert found[0].severity.value == "high"
        assert found[0].confidence.value == "certain"
        assert found[0].location.file.endswith("queries.py")

    def test_the_message_names_the_lookup_and_the_failure(self, tmp_path: pathlib.Path) -> None:
        message = findings(build(tmp_path, DIVERGENT, CONTAINS))[0].message
        assert "'data__contains'" in message
        assert "NotSupportedError" in message

    def test_django_s_own_feature_flag_is_the_evidence(self, tmp_path: pathlib.Path) -> None:
        # The claim is Django's. A reader who doubts it should be able to check
        # it in Django rather than take our word for it.
        found = findings(build(tmp_path, DIVERGENT, CONTAINS))
        flags = [e for e in found[0].evidence if e.source == "django feature flags"]
        assert len(flags) == 1
        assert "supports_json_field_contains = False" in flags[0].content

    def test_contained_by_is_the_same_failure(self, tmp_path: pathlib.Path) -> None:
        found = findings(
            build(tmp_path, DIVERGENT, query("Order.objects.filter(data__contained_by=value)"))
        )
        assert len(found) == 1
        assert found[0].properties["lookups"] == "data__contained_by"

    def test_a_key_inside_the_field_is_still_the_field(self, tmp_path: pathlib.Path) -> None:
        # `tags` is a key transform, not a column, so the path has to be
        # shortened before the model graph recognises anything. Measured on a
        # real SQLite: `data__tags__contains` raises exactly as `data__contains`
        # does, so stopping at the first unrecognised segment would miss it.
        found = findings(
            build(tmp_path, DIVERGENT, query("Order.objects.filter(data__tags__contains=value)"))
        )
        assert len(found) == 1
        assert found[0].properties["lookups"] == "data__tags__contains"
        assert found[0].properties["fields"] == "data"

    def test_a_json_field_on_a_related_model_is_reported(self, tmp_path: pathlib.Path) -> None:
        found = findings(
            build(
                tmp_path,
                DIVERGENT,
                query("Order.objects.filter(supplier__profile__contains=value)"),
            )
        )
        assert len(found) == 1
        assert found[0].properties["fields"] == "supplier__profile"

    def test_exclude_is_the_same_query(self, tmp_path: pathlib.Path) -> None:
        # Measured: `exclude(data__contains=...)` raises on SQLite too. The
        # lookup is compiled either way and negation happens above it.
        assert (
            len(
                findings(
                    build(tmp_path, DIVERGENT, query("Order.objects.exclude(data__contains=value)"))
                )
            )
            == 1
        )

    def test_two_bad_lookups_in_one_call_are_one_finding(self, tmp_path: pathlib.Path) -> None:
        found = findings(
            build(
                tmp_path,
                DIVERGENT,
                query(
                    "Order.objects.filter(data__contains=value, supplier__profile__contains=value)"
                ),
            )
        )
        assert len(found) == 1
        assert "'data__contains' and 'supplier__profile__contains'" in found[0].message

    def test_a_filter_deep_in_a_chain_is_still_found(self, tmp_path: pathlib.Path) -> None:
        # The tracker keys a chain at its outermost expression only, so a
        # `filter()` written before an `order_by()` is invisible without the
        # spine peeling that `Frame.queryset_models` does.
        found = findings(
            build(
                tmp_path,
                DIVERGENT,
                query("Order.objects.filter(data__contains=value).order_by('sku')"),
            )
        )
        assert len(found) == 1

    def test_get_is_a_lookup_method_too(self, tmp_path: pathlib.Path) -> None:
        # `get()` compiles the same lookup and fails the same way; a rule that
        # only watched `filter` would be silent on the shape most likely to be
        # written inside a view.
        assert (
            len(
                findings(
                    build(tmp_path, DIVERGENT, query("Order.objects.get(data__contains=value)"))
                )
            )
            == 1
        )

    def test_get_or_create_is_a_lookup_method_too(self, tmp_path: pathlib.Path) -> None:
        code = query("Order.objects.get_or_create(data__contains=value)")
        assert len(findings(build(tmp_path, DIVERGENT, code))) == 1

    def test_update_or_create_is_a_lookup_method_too(self, tmp_path: pathlib.Path) -> None:
        code = query("Order.objects.update_or_create(data__contains=value)")
        assert len(findings(build(tmp_path, DIVERGENT, code))) == 1

    def test_a_double_star_beside_the_lookup_does_not_stop_it(self, tmp_path: pathlib.Path) -> None:
        # `**extra` gives a keyword whose `arg` is None. Reading it as a string
        # raises, and the engine records rule exceptions rather than raising
        # them, so this would have become silence rather than a crash.
        code = (
            "from shop.models import Order\n\n\n"
            "def go(value, extra):\n"
            "    return Order.objects.filter(data__contains=value, **extra)\n"
        )
        assert len(findings(build(tmp_path, DIVERGENT, code))) == 1

    def test_an_ordinary_keyword_beside_the_lookup_is_skipped_not_read(
        self, tmp_path: pathlib.Path
    ) -> None:
        # `sku` has no containment suffix, so there is nothing to strip off it.
        # Falling through to the stripping anyway asks for the length of a
        # suffix that was never found.
        code = query("Order.objects.filter(data__contains=value, sku='x')")
        found = findings(build(tmp_path, DIVERGENT, code))
        assert len(found) == 1
        assert found[0].properties["lookups"] == "data__contains"

    def test_a_keyword_expansion_elsewhere_in_the_file_does_not_break_it(
        self, tmp_path: pathlib.Path
    ) -> None:
        # The textual prefilter means a file with only `**criteria` in it is
        # never parsed, so the None-keyword case is only reachable when some
        # other query in the same file mentions containment. That makes this
        # the shape that actually exercises the guard.
        code = (
            "from shop.models import Order\n\n\n"
            "def flexible(criteria):\n    return Order.objects.filter(**criteria)\n\n\n"
            "def fixed(value):\n    return Order.objects.filter(data__contains=value)\n"
        )
        found = findings(build(tmp_path, DIVERGENT, code))
        assert len(found) == 1
        assert found[0].location.line == 9


class TestWhenItStaysSilent:
    def test_a_postgres_only_project_is_not_reported(self, tmp_path: pathlib.Path) -> None:
        assert findings(build(tmp_path, POSTGRES_ONLY, CONTAINS)) == []

    def test_the_divergence_is_what_makes_it_reportable(self, tmp_path: pathlib.Path) -> None:
        # The control for the test above: the same file, reported or not purely
        # on the settings module.
        assert len(findings(build(tmp_path, DIVERGENT, CONTAINS))) == 1

    def test_contains_on_a_char_field_is_not_this_rule(self, tmp_path: pathlib.Path) -> None:
        # The whole reason this rule needs a model. `notes__contains` is an
        # ordinary substring match that runs on both backends; it differs in
        # case sensitivity, which is DJX-004's subject, not a crash.
        assert (
            findings(
                build(tmp_path, DIVERGENT, query("Order.objects.filter(notes__contains=value)"))
            )
            == []
        )

    def test_has_key_is_the_portable_question_and_is_not_reported(
        self, tmp_path: pathlib.Path
    ) -> None:
        # Measured on both backends: `has_key` returns the row on each. The
        # remediation names it, so a rule that reported it would be telling
        # people to do something it then complains about.
        assert (
            findings(build(tmp_path, DIVERGENT, query("Order.objects.filter(data__has_key=value)")))
            == []
        )

    def test_a_queryset_with_no_resolvable_model_is_not_reported(
        self, tmp_path: pathlib.Path
    ) -> None:
        # `self.get_queryset()` is a queryset the tracker recognises and cannot
        # name -- the one shape that reaches this rule with `model` as None.
        # Measured: a bare parameter is not tracked at all, so it never gets
        # here and cannot stand in for this case.
        code = (
            "class View:\n"
            "    def go(self, value):\n"
            "        return self.get_queryset().filter(data__contains=value)\n"
        )
        assert findings(build(tmp_path, DIVERGENT, code)) == []

    def test_a_queryset_we_cannot_follow_at_all_is_not_reported(
        self, tmp_path: pathlib.Path
    ) -> None:
        code = "def go(qs, value):\n    return qs.filter(data__contains=value)\n"
        assert findings(build(tmp_path, DIVERGENT, code)) == []

    def test_matching_a_json_field_exactly_is_portable(self, tmp_path: pathlib.Path) -> None:
        # Measured on both backends: `filter(data={'n': 1})` returns the row on
        # each. Only containment is missing from SQLite, so a rule that spoke
        # about the field rather than the lookup would be wrong here.
        assert findings(build(tmp_path, DIVERGENT, query("Order.objects.filter(data=value)"))) == []

    def test_a_key_lookup_inside_the_field_is_portable(self, tmp_path: pathlib.Path) -> None:
        # Also measured on both: `data__n=1` returns the row on each.
        assert (
            findings(build(tmp_path, DIVERGENT, query("Order.objects.filter(data__n=value)"))) == []
        )

    def test_only_a_keyword_expansion_is_not_reported(self, tmp_path: pathlib.Path) -> None:
        code = (
            "from shop.models import Order\n\n\n"
            "def go(criteria):\n    return Order.objects.filter(**criteria)\n"
        )
        assert findings(build(tmp_path, DIVERGENT, code)) == []

    def test_a_field_the_model_does_not_have_is_not_reported(self, tmp_path: pathlib.Path) -> None:
        assert (
            findings(
                build(tmp_path, DIVERGENT, query("Order.objects.filter(absent__contains=value)"))
            )
            == []
        )

    def test_an_annotation_keyword_is_not_a_lookup(self, tmp_path: pathlib.Path) -> None:
        # `annotate(data__contains=...)` is not even legal Django, but the
        # shape matters: `annotate`'s keywords name the annotation, so reading
        # them as field lookups is how a rule invents a column.
        code = query("Order.objects.annotate(data__contains=value)")
        assert findings(build(tmp_path, DIVERGENT, code)) == []

    def test_a_filter_that_is_not_a_queryset_is_not_reported(self, tmp_path: pathlib.Path) -> None:
        code = (
            "class Bag:\n"
            "    def filter(self, **kw):\n        return kw\n\n\n"
            "def go(value):\n    return Bag().filter(data__contains=value)\n"
        )
        assert findings(build(tmp_path, DIVERGENT, code)) == []


class TestItIsStillTheSameRule:
    def test_it_is_registered_under_the_portability_family(self) -> None:
        from djaudit.models import Family
        from djaudit.registry import get

        assert get("DJX-002").meta.family is Family.DJX

    def test_it_says_what_it_cannot_see(self) -> None:
        from djaudit.registry import get

        assert any("ArrayField" in limit for limit in get("DJX-002").meta.limitations)

    def test_the_remediation_names_a_lookup_that_works_on_both(self) -> None:
        from djaudit.registry import get

        assert "has_key" in get("DJX-002").meta.remediation

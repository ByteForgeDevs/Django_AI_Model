"""DJX-003 -- `distinct("field")` in a project that also runs SQLite.

The rule that most clearly shows what gates this family. `distinct('sku')` is
not a matter of degree: SQLite raises `NotSupportedError` when the queryset is
evaluated. But it is only a *defect* in a project that runs SQLite, and NetBox
writes three of them without having a problem, because NetBox runs Postgres and
nothing else. The construct and the divergence are two separate questions and
this rule requires both.
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
    "class Order(models.Model):\n    sku = models.CharField(max_length=32)\n"
)


def build(tmp_path: pathlib.Path, databases: str, code: str) -> pathlib.Path:
    root = tmp_path / f"proj{len(list(tmp_path.iterdir()))}"
    (root / "config").mkdir(parents=True)
    (root / "shop").mkdir()
    (root / "manage.py").write_text(
        "import os\nos.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')\n"
    )
    (root / "config" / "__init__.py").write_text("")
    (root / "config" / "settings.py").write_text(MARKERS + databases)
    (root / "shop" / "__init__.py").write_text("")
    (root / "shop" / "models.py").write_text(MODELS)
    (root / "shop" / "queries.py").write_text(code)
    return root


def findings(root: pathlib.Path) -> list[Finding]:
    """Every DJX-003 finding, having first checked the rule did not crash.

    The engine records a rule's exceptions rather than propagating them, so an
    unguarded `== []` assertion passes for a rule that never ran at all.
    """
    result = engine.run(root)
    assert "DJX-003" not in result.rule_errors, result.rule_errors.get("DJX-003")
    return [f for f in result.findings if f.rule_id == "DJX-003"]


DISTINCT_ON = (
    "from shop.models import Order\n\n\n"
    "def latest():\n    return Order.objects.order_by('sku').distinct('sku')\n"
)


class TestWhenItFires:
    def test_a_field_argument_on_a_divergent_project_is_reported(
        self, tmp_path: pathlib.Path
    ) -> None:
        found = findings(build(tmp_path, DIVERGENT, DISTINCT_ON))
        assert len(found) == 1
        assert found[0].severity.value == "high"
        assert found[0].confidence.value == "certain"
        assert found[0].location.file.endswith("queries.py")

    def test_the_message_names_the_field_and_the_failure(self, tmp_path: pathlib.Path) -> None:
        message = findings(build(tmp_path, DIVERGENT, DISTINCT_ON))[0].message
        assert "distinct('sku')" in message
        assert "DISTINCT ON" in message
        assert "NotSupportedError" in message

    def test_django_s_own_feature_flag_is_the_evidence(self, tmp_path: pathlib.Path) -> None:
        # The claim is Django's, not ours, and a reader who doubts it should be
        # able to check it in Django rather than take our word for it.
        found = findings(build(tmp_path, DIVERGENT, DISTINCT_ON))
        flags = [e for e in found[0].evidence if e.source == "django feature flags"]
        assert len(flags) == 1
        assert "can_distinct_on_fields = False" in flags[0].content

    def test_several_fields_are_all_named(self, tmp_path: pathlib.Path) -> None:
        code = (
            "from shop.models import Order\n\n\n"
            "def latest():\n    return Order.objects.order_by('sku').distinct('sku', 'id')\n"
        )
        found = findings(build(tmp_path, DIVERGENT, code))
        assert len(found) == 1
        assert "distinct('sku', 'id')" in found[0].message
        assert found[0].properties["fields"] == "sku id"

    def test_a_field_beside_an_expression_still_names_only_the_field(
        self, tmp_path: pathlib.Path
    ) -> None:
        # `distinct('sku', order)` is DISTINCT ON either way, so the finding
        # stands; but only the argument we can actually read is quoted back.
        # Naming a variable we have not resolved would be inventing evidence.
        code = (
            "from shop.models import Order\n\n\n"
            "def latest(order):\n"
            "    return Order.objects.order_by('sku').distinct('sku', order)\n"
        )
        found = findings(build(tmp_path, DIVERGENT, code))
        assert len(found) == 1
        assert "distinct('sku')" in found[0].message
        assert "order" not in found[0].properties["fields"]


class TestWhenItStaysSilent:
    def test_a_postgres_only_project_is_not_reported(self, tmp_path: pathlib.Path) -> None:
        # NetBox's actual situation. The construct is there and it is fine,
        # because there is no second engine for it to be wrong on.
        assert findings(build(tmp_path, POSTGRES_ONLY, DISTINCT_ON)) == []

    def test_the_divergence_is_what_makes_it_reportable(self, tmp_path: pathlib.Path) -> None:
        # The control for the test above, and the load-bearing claim of the
        # whole family: the same file, reported or not purely on the settings.
        assert len(findings(build(tmp_path, DIVERGENT, DISTINCT_ON))) == 1

    def test_distinct_with_no_arguments_is_portable(self, tmp_path: pathlib.Path) -> None:
        # The overwhelmingly common form, and the one Healthchecks writes twice.
        code = (
            "from shop.models import Order\n\n\n"
            "def all_of_them():\n    return Order.objects.distinct()\n"
        )
        assert findings(build(tmp_path, DIVERGENT, code)) == []

    def test_a_distinct_that_is_not_a_queryset_is_not_reported(
        self, tmp_path: pathlib.Path
    ) -> None:
        # Matching the method name alone would report any object with a
        # `distinct` method, which is a rule about spelling.
        code = (
            "class Report:\n"
            "    def distinct(self, field):\n        return field\n\n\n"
            "def go():\n    return Report().distinct('sku')\n"
        )
        assert findings(build(tmp_path, DIVERGENT, code)) == []

    def test_a_computed_argument_is_not_reported(self, tmp_path: pathlib.Path) -> None:
        # `distinct(*fields)` is DISTINCT ON only when `fields` is non-empty at
        # runtime, and we cannot know that. The family reports what it can
        # prove, so an unreadable argument buys silence rather than a guess.
        code = (
            "from shop.models import Order\n\n\n"
            "def latest(fields):\n    return Order.objects.distinct(*fields)\n"
        )
        assert findings(build(tmp_path, DIVERGENT, code)) == []

    def test_a_variable_argument_is_not_reported(self, tmp_path: pathlib.Path) -> None:
        code = (
            "from shop.models import Order\n\n\n"
            "def latest(field):\n    return Order.objects.distinct(field)\n"
        )
        assert findings(build(tmp_path, DIVERGENT, code)) == []


class TestItIsStillTheSameRule:
    def test_it_is_registered_under_the_portability_family(self) -> None:
        from djaudit.models import Family
        from djaudit.registry import get

        assert get("DJX-003").meta.family is Family.DJX

    def test_it_says_what_it_cannot_see(self) -> None:
        from djaudit.registry import get

        assert any("NetBox" in limit for limit in get("DJX-003").meta.limitations)

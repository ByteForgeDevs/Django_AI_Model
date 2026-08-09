"""DJX-007 -- ``select_for_update()`` compiles to nothing at all on SQLite.

The quietest defect in the family. Measured on a real SQLite and the live
Postgres, same model, same call:

    sqlite    SELECT "t_item"."id", "t_item"."sku" FROM "t_item"
    postgres  SELECT "t_item"."id", "t_item"."sku" FROM "t_item" FOR UPDATE

No exception, no warning, the same rows. ``nowait``, ``skip_locked`` and
``of=("self",)`` are discarded identically -- all four spellings produce the
same bare SELECT on SQLite and four different clauses on Postgres.

The second divergence was found while measuring the first. Django's whole
select-for-update block is gated on ``has_select_for_update``, including the
guard that rejects the call outside a transaction, so:

    outside atomic()   sqlite returns rows
                       postgres TransactionManagementError

which means the engine that never complains in development is the one that
cannot crash.
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
)


def build(tmp_path: pathlib.Path, databases: str, queries: str) -> pathlib.Path:
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
    (root / "shop" / "queries.py").write_text(queries)
    return root


def findings(root: pathlib.Path) -> list[Finding]:
    result = engine.run(root)
    assert "DJX-007" not in result.rule_errors, result.rule_errors.get("DJX-007")
    return [f for f in result.findings if f.rule_id == "DJX-007"]


def query(body: str) -> str:
    return (
        "from django.db import transaction\n\nfrom shop.models import Order\n\n\n"
        "def touch(sku):\n"
        "    with transaction.atomic():\n"
        f"{body}"
    )


LOCKED = query("        return Order.objects.select_for_update().get(sku=sku)\n")


class TestWhenItFires:
    def test_a_row_lock_is_reported(self, tmp_path: pathlib.Path) -> None:
        found = findings(build(tmp_path, DIVERGENT, LOCKED))
        assert len(found) == 1
        assert found[0].severity.value == "high"
        assert found[0].confidence.value == "certain"

    def test_it_names_the_model_being_locked(self, tmp_path: pathlib.Path) -> None:
        assert "locks shop.Order's rows" in findings(build(tmp_path, DIVERGENT, LOCKED))[0].message

    def test_the_whole_message_reads_as_one_sentence(self, tmp_path: pathlib.Path) -> None:
        assert findings(build(tmp_path, DIVERGENT, LOCKED))[0].message == (
            "select_for_update() emits no FOR UPDATE on SQLite, so nothing locks "
            "shop.Order's rows on the engine this project runs in development and "
            "the lock is only real in production"
        )

    def test_it_points_at_the_call(self, tmp_path: pathlib.Path) -> None:
        found = findings(build(tmp_path, DIVERGENT, LOCKED))
        assert found[0].location.file == "shop/queries.py"
        assert found[0].location.line == 8

    def test_the_evidence_shows_both_compilations(self, tmp_path: pathlib.Path) -> None:
        # The finding's whole claim is that one clause is missing. Evidence
        # that did not show the two SQL strings side by side would be asking
        # the reader to take it on trust.
        sql = [
            e
            for e in findings(build(tmp_path, DIVERGENT, LOCKED))[0].evidence
            if e.kind.value == "sql"
        ]
        assert len(sql) == 1
        assert sql[0].content == (
            "sqlite:   SELECT ... FROM t_item\npostgres: SELECT ... FROM t_item FOR UPDATE"
        )
        assert sql[0].source == "measured against django 6.0"

    def test_the_source_evidence_quotes_the_call(self, tmp_path: pathlib.Path) -> None:
        source = [
            e
            for e in findings(build(tmp_path, DIVERGENT, LOCKED))[0].evidence
            if e.kind.value == "source"
        ]
        assert len(source) == 1
        assert "select_for_update" in source[0].content
        assert source[0].source == "shop/queries.py"

    def test_a_view_s_own_queryset_is_reported_without_naming_a_model(
        self, tmp_path: pathlib.Path
    ) -> None:
        # `self.get_queryset()` is a queryset the tracker agrees about and
        # cannot name -- Origin.UNKNOWN carries the chain but no model. It is
        # also the commonest spelling in a DRF or CBV codebase, so the branch
        # that cannot say which model is locked is not a corner case, and the
        # message has to stay grammatical without one.
        code = (
            "from django.db import transaction\n\n\n"
            "class OrderView:\n"
            "    def touch(self):\n"
            "        with transaction.atomic():\n"
            "            return self.get_queryset().select_for_update().first()\n"
        )
        found = findings(build(tmp_path, DIVERGENT, code))
        assert len(found) == 1
        assert "nothing locks these rows on the engine" in found[0].message
        assert "None" not in found[0].message

    def test_two_locks_are_two_findings(self, tmp_path: pathlib.Path) -> None:
        code = query(
            "        Order.objects.select_for_update().get(sku=sku)\n"
            "        return Order.objects.select_for_update().first()\n"
        )
        assert len(findings(build(tmp_path, DIVERGENT, code))) == 2


class TestWhatWasAskedFor:
    """Measured: every keyword is discarded, and each one is a different ask.

    On Postgres the four spellings compile to ``FOR UPDATE``, ``FOR UPDATE
    NOWAIT``, ``FOR UPDATE SKIP LOCKED`` and ``FOR UPDATE OF "t_item"``. On
    SQLite all four compile to the same bare SELECT.
    """

    def test_skip_locked_is_named_in_the_message(self, tmp_path: pathlib.Path) -> None:
        code = query("        return Order.objects.select_for_update(skip_locked=True).first()\n")
        found = findings(build(tmp_path, DIVERGENT, code))
        assert "-- including skip_locked," in found[0].message
        assert found[0].properties["arguments"] == "skip_locked"

    def test_nowait_is_named_in_the_message(self, tmp_path: pathlib.Path) -> None:
        code = query("        return Order.objects.select_for_update(nowait=True).first()\n")
        assert findings(build(tmp_path, DIVERGENT, code))[0].properties["arguments"] == "nowait"

    def test_of_is_named_in_the_message(self, tmp_path: pathlib.Path) -> None:
        code = query("        return Order.objects.select_for_update(of=('self',)).first()\n")
        assert findings(build(tmp_path, DIVERGENT, code))[0].properties["arguments"] == "of"

    def test_no_key_is_named_in_the_message(self, tmp_path: pathlib.Path) -> None:
        code = query("        return Order.objects.select_for_update(no_key=True).first()\n")
        assert findings(build(tmp_path, DIVERGENT, code))[0].properties["arguments"] == "no_key"

    def test_several_arguments_are_listed_together(self, tmp_path: pathlib.Path) -> None:
        code = query(
            "        return Order.objects.select_for_update(nowait=True, of=('self',)).first()\n"
        )
        found = findings(build(tmp_path, DIVERGENT, code))
        assert "-- including nowait and of," in found[0].message
        assert found[0].properties["arguments"] == "nowait of"

    def test_a_plain_lock_says_nothing_about_arguments(self, tmp_path: pathlib.Path) -> None:
        # Presence control for the tests above: without a keyword the clause
        # is absent entirely rather than empty.
        found = findings(build(tmp_path, DIVERGENT, LOCKED))
        assert "including" not in found[0].message
        assert found[0].properties["arguments"] == ""

    def test_an_unrecognised_keyword_is_not_announced(self, tmp_path: pathlib.Path) -> None:
        # Django has no `wait=` argument. Echoing whatever was written would
        # let the finding assert something about Postgres that is not true.
        code = query("        return Order.objects.select_for_update(wait=True).first()\n")
        found = findings(build(tmp_path, DIVERGENT, code))
        assert len(found) == 1
        assert "including" not in found[0].message


class TestWhenItStaysSilent:
    def test_a_postgres_only_project_is_not_reported(self, tmp_path: pathlib.Path) -> None:
        assert findings(build(tmp_path, POSTGRES_ONLY, LOCKED)) == []

    def test_the_divergence_is_what_makes_it_reportable(self, tmp_path: pathlib.Path) -> None:
        assert len(findings(build(tmp_path, DIVERGENT, LOCKED))) == 1

    def test_a_plain_get_is_not_reported(self, tmp_path: pathlib.Path) -> None:
        assert (
            findings(
                build(tmp_path, DIVERGENT, query("        return Order.objects.get(sku=sku)\n"))
            )
            == []
        )

    def test_a_method_of_that_name_on_something_else_is_not_reported(
        self, tmp_path: pathlib.Path
    ) -> None:
        # The tracker has to agree this is a queryset. A lock manager of the
        # project's own with the same method name is not one.
        code = (
            "from shop.locks import manager\n\n\n"
            "def touch(sku):\n"
            "    return manager.select_for_update()\n"
        )
        root = build(tmp_path, DIVERGENT, code)
        (root / "shop" / "locks.py").write_text(
            "class Manager:\n"
            "    def select_for_update(self):\n"
            "        return None\n\n\n"
            "manager = Manager()\n"
        )
        assert findings(root) == []

    def test_the_queryset_control_proves_that_silence_is_aimed(
        self, tmp_path: pathlib.Path
    ) -> None:
        # Presence control for the test above: the same method name on
        # something the tracker does resolve to a model is reported.
        assert len(findings(build(tmp_path, DIVERGENT, LOCKED))) == 1


class TestItIsStillTheSameRule:
    def test_it_is_registered_under_the_portability_family(self) -> None:
        from djaudit.models import Family
        from djaudit.registry import get

        assert get("DJX-007").meta.family is Family.DJX

    def test_the_rationale_states_the_transaction_divergence_too(self) -> None:
        # Found while measuring the first one, and the more dangerous of the
        # two: the engine that never complains in development is the one that
        # cannot crash.
        from djaudit.registry import get

        rationale = get("DJX-007").meta.rationale
        assert "TransactionManagementError" in rationale
        assert "outside `atomic()`" in rationale

    def test_it_admits_it_does_not_read_the_transaction(self) -> None:
        # The rule reports the second divergence as a possibility because it
        # never checks whether the call is inside atomic(). Claiming it as
        # fact would be a promise the code does not keep.
        from djaudit.registry import get

        assert any(
            "inside `transaction.atomic()` is not read" in limit
            for limit in get("DJX-007").meta.limitations
        )

    def test_the_remediation_offers_a_fallback_that_both_engines_honour(self) -> None:
        from djaudit.registry import get

        remediation = get("DJX-007").meta.remediation
        assert "unique constraint" in remediation
        assert "version column" in remediation

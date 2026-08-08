"""Tests for `DJM-007`, and for the boundary between it and `DJP-007`.

The class that carries the design is `TestItIsTheFetchNotTheWrite`. Everything
else measures a guard.
"""

from __future__ import annotations

import textwrap

from djaudit.context import ProjectContext
from djaudit.models import Confidence, EvidenceKind, Family, Severity, Tier
from djaudit.rules.djm_unbounded_data_migration import UnboundedQuerysetInDataMigration
from tests.rules.test_djm_addfield_not_null import Build, findings_for

MODELS = """\
from django.db import models


class Order(models.Model):
    code = models.CharField(max_length=32)
    retries = models.IntegerField(default=0)
"""

INITIAL = """\
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    operations = [
        migrations.CreateModel(
            name='Order',
            fields=[('id', models.AutoField(primary_key=True, serialize=False))],
        ),
    ]
"""


def data_migration(body: str, operations: str | None = None, deps: str = "'0001_initial'") -> str:
    """A migration whose `RunPython` points at a module-level `forward`.

    ``body`` is the function body, dedented by the caller's triple-quote
    indentation and re-indented here, so a test can write the loop it is about
    and nothing else.
    """
    ops = operations if operations is not None else "migrations.RunPython(code=forward),"
    return (
        "from django.db import migrations, models\n\n\n"
        "def forward(apps, schema_editor):\n"
        f"{textwrap.indent(textwrap.dedent(body), '    ')}"
        "\n\nclass Migration(migrations.Migration):\n"
        f"    dependencies = [('shop', {deps})]\n"
        "    operations = [\n"
        f"{textwrap.indent(ops, ' ' * 8)}\n"
        "    ]\n"
    )


def project(build: Build, files: dict[str, str]) -> ProjectContext:
    base = {
        "manage.py": "import os\n",
        "settings.py": "INSTALLED_APPS = ['shop']\n",
        "shop/__init__.py": "",
        "shop/models.py": MODELS,
        "shop/migrations/__init__.py": "",
        "shop/migrations/0001_initial.py": INITIAL,
    }
    return build({**base, **files})


def loop(iterable: str) -> str:
    return f"""
    Order = apps.get_model('shop', 'Order')
    for order in {iterable}:
        order.retries = 0
"""


def findings(ctx: ProjectContext) -> list[str]:
    return findings_for(UnboundedQuerysetInDataMigration(), ctx)


def run(build: Build, iterable: str) -> list[str]:
    return findings(
        project(build, {"shop/migrations/0002_data.py": data_migration(loop(iterable))})
    )


class TestItIsTheFetchNotTheWrite:
    """The claim `DJP-007` does not make.

    `DJP-007` reports a loop that writes each row and asks for a
    `bulk_update`. That fix holds every row in memory, which is what this rule
    is about, so the two cannot be collapsed and the first two tests here say
    so directly: a loop with no write at all is still reported, and a loop that
    has already taken `DJP-007`'s advice is *especially* reported.
    """

    def test_reports_a_loop_that_never_writes(self, make_project: Build) -> None:
        body = """
        Order = apps.get_model('shop', 'Order')
        total = 0
        for order in Order.objects.all():
            total += order.retries
        """
        ctx = project(make_project, {"shop/migrations/0002_data.py": data_migration(body)})
        assert len(findings(ctx)) == 1

    def test_reports_a_loop_that_has_already_taken_djp_007s_advice(
        self, make_project: Build
    ) -> None:
        body = """
        Order = apps.get_model('shop', 'Order')
        updated = []
        for order in Order.objects.filter(retries__isnull=True):
            order.retries = 0
            updated.append(order)
        Order.objects.bulk_update(updated, ['retries'])
        """
        ctx = project(make_project, {"shop/migrations/0002_data.py": data_migration(body)})
        assert len(findings(ctx)) == 1

    def test_remediation_names_the_conflict_rather_than_hiding_it(self) -> None:
        remediation = UnboundedQuerysetInDataMigration.meta.remediation
        assert "DJP-007" in remediation
        assert "bulk_update" in remediation
        assert "iterator(chunk_size=...)" in remediation


class TestTheQuerysetIsUnbounded:
    def test_reports_all(self, make_project: Build) -> None:
        assert len(run(make_project, "Order.objects.all()")) == 1

    def test_reports_filter(self, make_project: Build) -> None:
        assert len(run(make_project, "Order.objects.filter(retries=0)")) == 1

    def test_reports_the_bare_manager(self, make_project: Build) -> None:
        assert len(run(make_project, "Order.objects.exclude(code='')")) == 1

    def test_reports_a_queryset_reached_through_a_local_variable(self, make_project: Build) -> None:
        body = """
        Order = apps.get_model('shop', 'Order')
        qs = Order.objects.filter(retries=0)
        for order in qs:
            order.retries = 1
        """
        ctx = project(make_project, {"shop/migrations/0002_data.py": data_migration(body)})
        assert len(findings(ctx)) == 1

    def test_reports_a_list_around_an_iterator_because_list_undoes_it(
        self, make_project: Build
    ) -> None:
        """`.iterator()` streams; `list()` around it puts everything back in memory.

        The guard walks the attribute chain, so it has to stop at the `list`
        call rather than reaching past it and finding the `.iterator` behind.
        """
        assert len(run(make_project, "list(Order.objects.all().iterator())")) == 1


class TestTheBoundedShapesAreQuiet:
    def test_iterator_is_the_remediation_and_is_not_reported(self, make_project: Build) -> None:
        assert run(make_project, "Order.objects.all().iterator(chunk_size=500)") == []

    def test_aiterator_is_the_same_promise(self, make_project: Build) -> None:
        assert run(make_project, "Order.objects.all().aiterator()") == []

    def test_iterator_earlier_in_the_chain_still_counts(self, make_project: Build) -> None:
        assert run(make_project, "Order.objects.iterator(chunk_size=100)") == []

    def test_a_slice_caps_the_rows(self, make_project: Build) -> None:
        assert run(make_project, "Order.objects.all()[:500]") == []

    def test_a_plain_list_is_not_a_table(self, make_project: Build) -> None:
        body = """
        codes = ['a', 'b', 'c']
        for code in codes:
            print(code)
        """
        ctx = project(make_project, {"shop/migrations/0002_data.py": data_migration(body)})
        assert findings(ctx) == []

    def test_a_module_level_tuple_of_models_is_not_a_table(self, make_project: Build) -> None:
        """NetBox's leaf data migration, reduced.

        `dcim.0241_nullify_empty_cable_end` loops over a six-entry tuple of
        model classes. A rule that assumed every loop in a migration walked a
        table would report it, so this shape is measured rather than trusted.
        """
        module = data_migration("""
        for model in CABLED_MODELS:
            print(model)
        """).replace("def forward(", "CABLED_MODELS = ('Cable', 'Interface')\n\n\ndef forward(")
        ctx = project(make_project, {"shop/migrations/0002_data.py": module})
        assert findings(ctx) == []

    def test_a_terminal_query_returns_one_value_and_is_not_a_table(
        self, make_project: Build
    ) -> None:
        """Found by a surviving mutant rather than by review.

        An earlier draft reported `for k in Order.objects.aggregate(...)` and
        `for x in Order.objects.count()`, because the syntactic fallback saw
        `.objects` and stopped asking. Both return a single value. The `and`
        that rejects them is what the mutation run could not kill, which is
        how the false positive surfaced at all.
        """
        assert run(make_project, "Order.objects.aggregate(n=Sum('retries'))") == []
        assert run(make_project, "Order.objects.count()") == []
        assert run(make_project, "Order.objects.filter(code='x').exists()") == []

    def test_values_is_not_terminal_and_is_still_every_row(self, make_project: Build) -> None:
        """The contrast that keeps the terminal check from swallowing the rule."""
        assert len(run(make_project, "Order.objects.values('code')")) == 1
        assert len(run(make_project, "Order.objects.values_list('code', flat=True)")) == 1

    def test_a_manager_named_something_else_is_left_alone(self, make_project: Build) -> None:
        """The documented quiet direction.

        `Order.published.all()` is exactly as unbounded as
        `Order.objects.all()`, but treating every attribute followed by
        `.all()` as a manager would report any object with a collection on it.
        The limitation says so; this test holds the rule to it.
        """
        assert run(make_project, "Order.published.all()") == []

    def test_the_private_manager_names_are_recognised(self, make_project: Build) -> None:
        """`_default_manager` and `_base_manager` are what generated code uses.

        Both go through the syntactic fallback rather than the def-use chains,
        so the model is deliberately one the graph does not know -- otherwise
        the dataflow would report these on its own and the fallback's two extra
        names would not be measured at all.
        """
        module = (
            "from django.db import migrations, models\n\n\n"
            "def default(apps, schema_editor):\n"
            "    Store = apps.get_model('shop', 'SettingsStore')\n"
            "    for row in Store._default_manager.all():\n"
            "        row.value = ''\n\n\n"
            "def base(apps, schema_editor):\n"
            "    Store = apps.get_model('shop', 'SettingsStore')\n"
            "    for row in Store._base_manager.all():\n"
            "        row.value = ''\n\n\n"
            "class Migration(migrations.Migration):\n"
            "    dependencies = [('shop', '0001_initial')]\n"
            "    operations = [\n"
            "        migrations.RunPython(code=default),\n"
            "        migrations.RunPython(code=base),\n"
            "    ]\n"
        )
        ctx = project(make_project, {"shop/migrations/0002_data.py": module})
        messages = findings(ctx)
        assert len(messages) == 2
        assert any("_default_manager" in m for m in messages)
        assert any("_base_manager" in m for m in messages)


class TestOnlyTheMigrationsForwardFunction:
    def test_a_loop_in_another_function_in_the_same_file_is_not_attributed(
        self, make_project: Build
    ) -> None:
        module = data_migration(loop("Order.objects.all()")).replace(
            "\n\nclass Migration",
            "\n\ndef helper(apps):\n"
            "    Order = apps.get_model('shop', 'Order')\n"
            "    for order in Order.objects.all():\n"
            "        order.retries = 0\n"
            "\n\nclass Migration",
        )
        ctx = project(make_project, {"shop/migrations/0002_data.py": module})
        assert len(findings(ctx)) == 1

    def test_only_the_named_function_is_read(self, make_project: Build) -> None:
        module = data_migration(
            "    pass\n", operations="migrations.RunPython(code=forward),"
        ).replace(
            "\n\nclass Migration",
            "\n\ndef other(apps):\n"
            "    Order = apps.get_model('shop', 'Order')\n"
            "    for order in Order.objects.all():\n"
            "        order.retries = 0\n"
            "\n\nclass Migration",
        )
        ctx = project(make_project, {"shop/migrations/0002_data.py": module})
        assert findings(ctx) == []

    def test_a_dotted_callable_matches_on_its_last_component(self, make_project: Build) -> None:
        module = (
            "from django.db import migrations, models\n\n\n"
            "class Backfill:\n"
            "    def run(self, apps, schema_editor):\n"
            "        Order = apps.get_model('shop', 'Order')\n"
            "        for order in Order.objects.all():\n"
            "            order.retries = 0\n"
            "\n\nclass Migration(migrations.Migration):\n"
            "    dependencies = [('shop', '0001_initial')]\n"
            "    operations = [migrations.RunPython(code=Backfill.run)]\n"
        )
        ctx = project(make_project, {"shop/migrations/0002_data.py": module})
        assert len(findings(ctx)) == 1

    def test_two_operations_naming_one_function_report_once(self, make_project: Build) -> None:
        """A migration may run the same pass twice; the loop is still one loop."""
        module = data_migration(
            loop("Order.objects.all()"),
            operations=("migrations.RunPython(code=forward),\nmigrations.RunPython(code=forward),"),
        )
        ctx = project(make_project, {"shop/migrations/0002_data.py": module})
        assert len(findings(ctx)) == 1

    def test_a_run_python_inside_separate_database_and_state_is_still_read(
        self, make_project: Build
    ) -> None:
        module = data_migration(
            loop("Order.objects.all()"),
            operations=(
                "migrations.SeparateDatabaseAndState(\n"
                "    database_operations=[migrations.RunPython(code=forward)],\n"
                "),"
            ),
        )
        ctx = project(make_project, {"shop/migrations/0002_data.py": module})
        assert len(findings(ctx)) == 1

    def test_run_sql_is_not_this_rules_business(self, make_project: Build) -> None:
        module = data_migration(
            loop("Order.objects.all()"),
            operations="migrations.RunSQL(sql='UPDATE shop_order SET retries = 0'),",
        )
        ctx = project(make_project, {"shop/migrations/0002_data.py": module})
        assert findings(ctx) == []


class TestScope:
    def test_a_superseded_migration_is_out_of_scope(self, make_project: Build) -> None:
        """The family's central decision, measured here rather than assumed.

        `0002` holds the loop and `0003` depends on it, so `0002` is no longer
        a leaf: it shipped, it ran, and reporting it now tells nobody anything
        they can act on.
        """
        later = (
            "from django.db import migrations\n\n\n"
            "class Migration(migrations.Migration):\n"
            "    dependencies = [('shop', '0002_data')]\n"
            "    operations = []\n"
        )
        ctx = project(
            make_project,
            {
                "shop/migrations/0002_data.py": data_migration(loop("Order.objects.all()")),
                "shop/migrations/0003_later.py": later,
            },
        )
        assert findings(ctx) == []

    def test_the_leaf_is_reported(self, make_project: Build) -> None:
        assert len(run(make_project, "Order.objects.all()")) == 1


class TestTheMessageAndEvidence:
    def test_the_message_names_the_model_when_the_dataflow_resolved_it(
        self, make_project: Build
    ) -> None:
        body = """
        Order = apps.get_model('shop', 'Order')
        qs = Order.objects.all()
        for order in qs:
            order.retries = 0
        """
        ctx = project(make_project, {"shop/migrations/0002_data.py": data_migration(body)})
        (message,) = findings(ctx)
        # `ast.unparse` of the iterable would say `qs`, which names nothing a
        # reader can act on.
        assert "rows of `shop.Order`" in message
        assert "`qs`" not in message

    def test_the_message_falls_back_to_the_expression_when_the_model_is_unknown(
        self, make_project: Build
    ) -> None:
        body = """
        Store = apps.get_model('shop', 'SettingsStore')
        for row in Store.objects.filter(key='x'):
            row.value = ''
        """
        ctx = project(make_project, {"shop/migrations/0002_data.py": data_migration(body)})
        (message,) = findings(ctx)
        assert "`Store.objects.filter(key='x')`" in message

    def test_the_message_names_the_migration_and_the_function(self, make_project: Build) -> None:
        (message,) = run(make_project, "Order.objects.all()")
        assert "`shop.0002_data`" in message
        assert "in `forward`" in message
        assert "iterator(chunk_size=...)" in message

    def test_the_evidence_quotes_the_loop_line(self, make_project: Build) -> None:
        ctx = project(
            make_project,
            {"shop/migrations/0002_data.py": data_migration(loop("Order.objects.all()"))},
        )
        (finding,) = list(UnboundedQuerysetInDataMigration().check(ctx))
        assert finding.evidence[0].kind is EvidenceKind.AST
        assert finding.evidence[0].content == "for order in Order.objects.all():"
        line = finding.location.line
        assert finding.evidence[0].source == f"shop/migrations/0002_data.py:{line}"
        assert finding.evidence[1].source == f"shop/migrations/0002_data.py:{line}"

    def test_the_evidence_records_which_path_resolved_the_queryset(
        self, make_project: Build
    ) -> None:
        """Both paths are load-bearing on the corpus -- one pretix finding each.

        `banktransfer.0012` comes through the def-use chains; `returnurl.0002`
        only through the syntactic fallback, because its hierarkey model is
        absent from `models.py`. If these two strings ever collapsed into one,
        a reader could no longer tell which.

        Both shapes go in one migration on purpose. `make_project` writes every
        project it is handed into the same directory, so building two projects
        and comparing them would have compared the second against itself.
        """
        module = (
            "from django.db import migrations, models\n\n\n"
            "def known(apps, schema_editor):\n"
            "    Order = apps.get_model('shop', 'Order')\n"
            "    for order in Order.objects.all():\n"
            "        order.retries = 0\n\n\n"
            "def unknown(apps, schema_editor):\n"
            "    Store = apps.get_model('shop', 'SettingsStore')\n"
            "    for row in Store.objects.all():\n"
            "        row.value = ''\n\n\n"
            "class Migration(migrations.Migration):\n"
            "    dependencies = [('shop', '0001_initial')]\n"
            "    operations = [\n"
            "        migrations.RunPython(code=known),\n"
            "        migrations.RunPython(code=unknown),\n"
            "    ]\n"
        )
        ctx = project(make_project, {"shop/migrations/0002_data.py": module})
        by_function = {
            "known" if "in `known`" in f.message else "unknown": f.evidence[1].content
            for f in UnboundedQuerysetInDataMigration().check(ctx)
        }
        assert by_function["known"] == "queryset reached by dataflow; no iterator() and no slice"
        assert by_function["unknown"] == (
            "queryset reached by manager attribute; no iterator() and no slice"
        )

    def test_the_location_points_at_the_loop(self, make_project: Build) -> None:
        source = data_migration(loop("Order.objects.all()"))
        ctx = project(make_project, {"shop/migrations/0002_data.py": source})
        (finding,) = list(UnboundedQuerysetInDataMigration().check(ctx))
        expected = next(
            i for i, line in enumerate(source.splitlines(), 1) if line.strip().startswith("for ")
        )
        assert finding.location.line == expected


class TestMeta:
    def test_identity(self) -> None:
        meta = UnboundedQuerysetInDataMigration.meta
        assert meta.id == "DJM-007"
        assert meta.family is Family.DJM
        assert meta.tier is Tier.STATIC
        assert meta.severity is Severity.MEDIUM
        assert meta.confidence is Confidence.FIRM

    def test_the_leaf_scope_caveat_is_declared(self) -> None:
        assert any(
            "leaf" in limitation and "django_migrations" in limitation
            for limitation in UnboundedQuerysetInDataMigration.meta.limitations
        )

    def test_the_row_count_is_declared_unknowable(self) -> None:
        assert any(
            "row count" in limitation
            for limitation in UnboundedQuerysetInDataMigration.meta.limitations
        )

    def test_the_custom_manager_gap_is_declared(self) -> None:
        assert any(
            "custom manager" in limitation
            for limitation in UnboundedQuerysetInDataMigration.meta.limitations
        )

    def test_the_helper_function_gap_is_declared(self) -> None:
        assert any(
            "helper" in limitation
            for limitation in UnboundedQuerysetInDataMigration.meta.limitations
        )

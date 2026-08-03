"""The model graph against Django's own record of the same models.

``scripts/graph_coverage.py`` replays a project's migrations and compares the
result with what the graph found. That makes the migration replay itself load
bearing: if it invents a column, the gate fails on a correct graph, and if it
loses one, the gate passes on a broken one. So the replay is tested against
migrations written the way Django writes them, and the attribution -- the part
that decides whether a shortfall is explainable or a bug -- is tested against
each shape it claims to recognise.

The measurements against the real targets live in ``benchmarks/*-graph.json``
and run in CI, where the projects are cloned. Nothing here needs them.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from graph_coverage import Coverage, app_label, check, measure, replay  # noqa: E402


def write(root: Path, relative: str, source: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source).lstrip())
    return path


@pytest.fixture
def project(tmp_path):
    """A Django project laid out the way the discovery pass expects."""
    write(tmp_path, "manage.py", "import django\n")
    write(
        tmp_path,
        "config/settings.py",
        """
        INSTALLED_APPS = ["shop"]
        ROOT_URLCONF = "config.urls"
        """,
    )
    write(tmp_path, "config/__init__.py", "")
    write(tmp_path, "shop/__init__.py", "")
    write(tmp_path, "shop/migrations/__init__.py", "")
    return tmp_path


def migration(root: Path, name: str, operations: str) -> None:
    write(
        root,
        f"shop/migrations/{name}.py",
        f"""
        from django.db import migrations, models


        class Migration(migrations.Migration):
            operations = [
        {textwrap.indent(textwrap.dedent(operations).strip(), " " * 8)}
            ]
        """,
    )


class TestReplay:
    """Every operation that changes what a table has in it."""

    def test_create_model_records_its_fields(self, project):
        migration(
            project,
            "0001_initial",
            """
            migrations.CreateModel(
                name='Order',
                fields=[
                    ('id', models.AutoField(primary_key=True)),
                    ('total', models.DecimalField()),
                ],
            ),
            """,
        )
        models = replay(project)["shop"]
        assert models["order"].name == "Order"
        assert models["order"].fields == {"id", "total"}

    def test_fields_added_and_removed_later_are_applied(self, project):
        migration(
            project,
            "0001_initial",
            "migrations.CreateModel(name='Order', fields=[('id', models.AutoField())]),",
        )
        migration(
            project,
            "0002_note",
            """
            migrations.AddField(model_name='order', name='note', field=models.CharField()),
            migrations.RemoveField(model_name='order', name='id'),
            """,
        )
        assert replay(project)["shop"]["order"].fields == {"note"}

    def test_renames_follow_the_row_rather_than_the_name(self, project):
        migration(
            project,
            "0001_initial",
            "migrations.CreateModel(name='Order', fields=[('total', models.DecimalField())]),",
        )
        migration(
            project,
            "0002_rename",
            """
            migrations.RenameField(
                model_name='order', old_name='total', new_name='amount'
            ),
            migrations.RenameModel(old_name='Order', new_name='Purchase'),
            """,
        )
        models = replay(project)["shop"]
        assert "order" not in models
        assert models["purchase"].fields == {"amount"}

    def test_a_deleted_model_is_gone(self, project):
        migration(
            project,
            "0001_initial",
            "migrations.CreateModel(name='Order', fields=[]),",
        )
        migration(project, "0002_drop", "migrations.DeleteModel(name='Order'),")
        assert replay(project)["shop"] == {}

    def test_positional_arguments_are_read_too(self, project):
        """Hand-edited migrations and squashes are full of them."""
        migration(
            project,
            "0001_initial",
            """
            migrations.CreateModel('Order', [('total', models.DecimalField())]),
            migrations.AddField('order', 'note', models.CharField()),
            """,
        )
        assert replay(project)["shop"]["order"].fields == {"total", "note"}

    def test_a_proxy_is_marked(self, project):
        migration(
            project,
            "0001_initial",
            """
            migrations.CreateModel(
                name='Staff', fields=[], options={'proxy': True, 'indexes': []}
            ),
            """,
        )
        assert replay(project)["shop"]["staff"].proxy


class TestAppLabel:
    """The label Django files the models under, which is not the folder name."""

    def test_the_directory_name_is_the_default(self, project):
        migration(project, "0001_initial", "migrations.CreateModel(name='Order', fields=[]),")
        assert set(replay(project)) == {"shop"}

    def test_an_explicit_label_in_apps_py_wins(self, project):
        """pretix's ``src/pretix/base`` is ``pretixbase`` everywhere but on disk.

        Reading the folder name instead put 100 of its 113 models in an app
        the graph had never heard of, and reported a working graph as 11%.
        """
        write(
            project,
            "shop/apps.py",
            """
            from django.apps import AppConfig


            class ShopConfig(AppConfig):
                name = 'shop'
                label = 'storefront'
            """,
        )
        migration(project, "0001_initial", "migrations.CreateModel(name='Order', fields=[]),")
        assert set(replay(project)) == {"storefront"}

    def test_an_app_with_no_apps_py_is_not_a_failure(self, tmp_path):
        assert app_label(tmp_path) == tmp_path.name


class TestMeasure:
    """What the comparison says about a project we can see all of."""

    def test_a_readable_project_is_fully_covered(self, project):
        write(
            project,
            "shop/models.py",
            """
            from django.db import models


            class Order(models.Model):
                total = models.DecimalField(max_digits=8, decimal_places=2)
                note = models.CharField(max_length=50)
            """,
        )
        migration(
            project,
            "0001_initial",
            """
            migrations.CreateModel(
                name='Order',
                fields=[
                    ('id', models.AutoField(primary_key=True)),
                    ('total', models.DecimalField()),
                    ('note', models.CharField()),
                ],
            ),
            """,
        )
        out = measure(project)
        assert (out.models_found, out.models_expected) == (1, 1)
        assert (out.fields_found, out.fields_expected) == (2, 2)
        assert out.models_unexplained == []
        assert out.fields_unexplained == []

    def test_the_generated_id_column_is_not_counted(self, project):
        """It is Django's, not the author's, and a model with its own primary
        key has none -- counting it would measure Django's behaviour."""
        write(
            project,
            "shop/models.py",
            """
            from django.db import models


            class Order(models.Model):
                total = models.DecimalField(max_digits=8, decimal_places=2)
            """,
        )
        migration(
            project,
            "0001_initial",
            """
            migrations.CreateModel(
                name='Order',
                fields=[('id', models.AutoField()), ('total', models.DecimalField())],
            ),
            """,
        )
        assert measure(project).fields_expected == 1

    def test_fields_from_a_base_outside_the_project_are_accounted_for(self, project):
        """NetBox's five MPTT columns, in miniature.

        The base is two levels up and in a package we do not have, so the
        columns exist and cannot be read. That is the graph working correctly,
        and the gate has to say so rather than count it against us.
        """
        write(
            project,
            "shop/models.py",
            """
            from django.db import models
            from mptt.models import MPTTModel


            class Node(MPTTModel, models.Model):
                class Meta:
                    abstract = True


            class Category(Node):
                name = models.CharField(max_length=50)
            """,
        )
        migration(
            project,
            "0001_initial",
            """
            migrations.CreateModel(
                name='Category',
                fields=[
                    ('id', models.AutoField()),
                    ('name', models.CharField()),
                    ('lft', models.PositiveIntegerField()),
                    ('rght', models.PositiveIntegerField()),
                ],
            ),
            """,
        )
        out = measure(project)
        assert out.fields_unexplained == []
        assert out.fields_unreadable_base["MPTTModel"] == 2

    def test_a_model_whose_only_base_is_third_party_is_accounted_for(self, project):
        """django-taggit's ``TaggedItem``: nothing in the source says model."""
        write(
            project,
            "shop/models.py",
            """
            from taggit.models import GenericTaggedItemBase


            class TaggedItem(GenericTaggedItemBase):
                pass
            """,
        )
        migration(project, "0001_initial", "migrations.CreateModel(name='TaggedItem', fields=[]),")
        out = measure(project)
        assert out.models_unexplained == []
        assert out.models_unreadable_base == [
            "shop.TaggedItem: inherits a base outside the project"
        ]

    def test_a_model_with_no_class_statement_is_accounted_for(self, project):
        """pretix's ``Event_SettingsStore``, built by a decorator at import."""
        write(project, "shop/models.py", "from django.db import models\n")
        migration(
            project,
            "0001_initial",
            "migrations.CreateModel(name='Event_SettingsStore', fields=[]),",
        )
        out = measure(project)
        assert out.models_unexplained == []
        assert "generated while the module imports" in out.models_unreadable_base[0]

    def test_a_model_we_simply_missed_is_not_explained_away(self, project):
        """The assertion the whole script exists to make.

        The class is declared, in the project, with a base that reaches
        ``models.Model`` -- everything needed to find it is on disk. It is in
        ``tables.py`` rather than ``models.py``, which Django permits and the
        graph does not look in, so the graph does not have it. Nothing in the
        attribution may make that acceptable: an excuse that covers a real gap
        is worse than no gate at all.
        """
        write(project, "shop/models.py", "from django.db import models\n")
        write(
            project,
            "shop/tables.py",
            """
            from django.db import models


            class Order(models.Model):
                total = models.DecimalField(max_digits=8, decimal_places=2)

                class Meta:
                    app_label = 'shop'
            """,
        )
        migration(project, "0001_initial", "migrations.CreateModel(name='Order', fields=[]),")
        out = measure(project)
        assert out.models_unexplained == ["shop.Order"]
        assert out.models_unreadable_base == []


class TestRelations:
    """What an edge points at, and what counts as failing to say."""

    def test_a_self_reference_and_a_generic_key_are_not_failures(self, project):
        """``to='self'`` names the declaring model, and a GenericForeignKey
        names nothing at all -- it is two columns read together. Counting
        either as unresolved invents a failure: it put 19 of NetBox's edges in
        the missing column and made a complete graph look 4% short."""
        write(
            project,
            "shop/models.py",
            """
            from django.contrib.contenttypes.fields import GenericForeignKey
            from django.db import models


            class Category(models.Model):
                parent = models.ForeignKey('self', on_delete=models.CASCADE, null=True)
                content_type = models.ForeignKey(
                    'contenttypes.ContentType', on_delete=models.CASCADE
                )
                object_id = models.PositiveIntegerField()
                subject = GenericForeignKey('content_type', 'object_id')
            """,
        )
        migration(project, "0001_initial", "migrations.CreateModel(name='Category', fields=[]),")
        out = measure(project)
        assert out.relations_resolved == 1
        # The ContentType edge is real, names a model Django ships, and is
        # reported as pointing outside the project rather than as a defect.
        assert dict(out.relations_outside) == {"contenttypes.ContentType": 1}


class TestGate:
    """Coverage may rise and may not fall."""

    def test_matching_the_record_passes(self):
        out = Coverage(models_found=10, fields_found=20, relations_resolved=5)
        assert check(out, {"models_found": 10, "fields_found": 20, "relations_resolved": 5}) == []

    def test_finding_more_than_the_record_passes(self):
        out = Coverage(models_found=11, fields_found=25, relations_resolved=6)
        assert check(out, {"models_found": 10, "fields_found": 20, "relations_resolved": 5}) == []

    def test_finding_less_than_the_record_fails(self):
        out = Coverage(models_found=9, fields_found=20, relations_resolved=5)
        assert check(out, {"models_found": 10}) == ["models_found fell from 10 to 9"]

    def test_a_new_unexplained_model_fails(self):
        out = Coverage(models_unexplained=["shop.Order"])
        assert check(out, {"models_unexplained": 0}) == ["models_unexplained rose from 0 to 1"]

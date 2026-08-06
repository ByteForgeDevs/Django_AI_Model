"""DJP-008 -- a whole table read into memory.

The rule's precision rests entirely on the context test, so most of these
tests are about *where* the code is rather than what it does. The same three
lines are reported inside a `RunPython` target and declined inside a sibling
function of the same migration file, because the claim being made is "Django
will run this against the production table", and only one of the two is.
"""

from __future__ import annotations

import textwrap

from djaudit.context import ProjectContext
from djaudit.models import Finding
from djaudit.registry import all_rules

SETTINGS = """
SECRET_KEY = "x"
DEBUG = False
ALLOWED_HOSTS = ["example.com"]
INSTALLED_APPS = ["library"]
ROOT_URLCONF = "library.urls"
"""

MODELS = """
from django.db import models


class Book(models.Model):
    title = models.CharField(max_length=200)
    sold = models.IntegerField(default=0)
"""

MIGRATION = """
from django.db import migrations

from library.models import Book

{body}

class Migration(migrations.Migration):
    dependencies = []
    operations = [migrations.RunPython(forwards)]
"""


def findings(make_project, files: dict[str, str]) -> list[Finding]:
    tree = {
        "manage.py": "import os\n",
        "library/__init__.py": "",
        "library/settings.py": SETTINGS,
        "library/urls.py": "urlpatterns = []\n",
        "library/models.py": MODELS,
    }
    tree.update({name: textwrap.dedent(code).lstrip() for name, code in files.items()})
    ctx: ProjectContext = make_project(tree)
    rule = next(r for r in all_rules() if r.meta.id == "DJP-008")
    return list(rule().check(ctx))


def in_migration(make_project, body: str) -> list[Finding]:
    return findings(
        make_project,
        {"library/migrations/0002_backfill.py": MIGRATION.format(body=textwrap.dedent(body))},
    )


class TestTheContextsItSpeaksAbout:
    def test_a_runpython_target(self, make_project):
        found = in_migration(
            make_project,
            """
            def forwards(apps, schema_editor):
                for book in list(Book.objects.all()):
                    book.title = book.title.strip()
            """,
        )
        assert len(found) == 1
        assert found[0].properties["context"] == "a data migration"
        assert found[0].properties["model"] == "library.Book"
        assert found[0].properties["materialiser"] == "list()"

    def test_a_comprehension_in_a_runpython_target(self, make_project):
        found = in_migration(
            make_project,
            """
            def forwards(apps, schema_editor):
                return [book.title for book in Book.objects.all()]
            """,
        )
        assert len(found) == 1
        assert found[0].properties["materialiser"] == "a list comprehension"

    def test_a_management_command(self, make_project):
        found = findings(
            make_project,
            {
                "library/management/__init__.py": "",
                "library/management/commands/__init__.py": "",
                "library/management/commands/backfill.py": """
                from django.core.management.base import BaseCommand

                from library.models import Book


                class Command(BaseCommand):
                    def handle(self, *args, **options):
                        titles = sorted(Book.objects.all())
                        return len(titles)
                """,
            },
        )
        assert len(found) == 1
        assert found[0].properties["context"] == "a management command"
        assert found[0].properties["materialiser"] == "sorted()"

    def test_a_scheduled_task(self, make_project):
        found = findings(
            make_project,
            {
                "library/tasks.py": """
                from celery import shared_task

                from library.models import Book


                @shared_task
                def reindex():
                    return {book.pk: book.title for book in Book.objects.all()}
                """,
            },
        )
        assert len(found) == 1
        assert found[0].properties["context"] == "a scheduled task"
        assert found[0].properties["materialiser"] == "a dict comprehension"

    def test_a_helper_defined_inside_the_target(self, make_project):
        """A nested function inherits its parent's context -- Django runs it
        just the same."""
        found = in_migration(
            make_project,
            """
            def forwards(apps, schema_editor):
                def load():
                    return set(Book.objects.all())

                return load()
            """,
        )
        assert len(found) == 1


class TestTheContextsItDeclines:
    def test_a_sibling_function_in_the_same_migration(self, make_project):
        """The whole point of reading `RunPython`'s arguments rather than the
        path: this file *is* a migration, and this function is still never run
        against the table."""
        found = in_migration(
            make_project,
            """
            def helper():
                return list(Book.objects.all())


            def forwards(apps, schema_editor):
                return None
            """,
        )
        assert found == []

    def test_a_name_passed_to_some_other_call(self, make_project):
        """`RunPython` is read by name, not "appears as an argument somewhere".
        A function handed to any other callable is not a migration step."""
        found = findings(
            make_project,
            {
                "library/service.py": """
                from library.models import Book

                def load():
                    return list(Book.objects.all())

                register(load)
                """,
            },
        )
        assert found == []

    def test_runpython_given_the_target_as_a_keyword(self, make_project):
        found = findings(
            make_project,
            {
                "library/migrations/0002_backfill.py": """
                from django.db import migrations

                from library.models import Book


                def forwards(apps, schema_editor):
                    return list(Book.objects.all())


                class Migration(migrations.Migration):
                    dependencies = []
                    operations = [migrations.RunPython(code=forwards)]
                """,
            },
        )
        assert len(found) == 1

    def test_runpython_given_a_target_from_another_module(self, make_project):
        found = findings(
            make_project,
            {
                "library/backfills.py": """
                from library.models import Book

                def forwards(apps, schema_editor):
                    return list(Book.objects.all())
                """,
                "library/migrations/0002_backfill.py": """
                from django.db import migrations

                from library import backfills


                class Migration(migrations.Migration):
                    dependencies = []
                    operations = [migrations.RunPython(backfills.forwards)]
                """,
            },
        )
        assert found == []

    def test_a_plain_module_function(self, make_project):
        found = findings(
            make_project,
            {
                "library/service.py": """
                from library.models import Book

                def load():
                    return list(Book.objects.all())
                """,
            },
        )
        assert found == []

    def test_a_view(self, make_project):
        """A table small enough to render is small enough to hold."""
        found = findings(
            make_project,
            {
                "library/views.py": """
                from django.shortcuts import render

                from library.models import Book

                def index(request):
                    return render(request, "i.html", {"books": list(Book.objects.all())})
                """,
            },
        )
        assert found == []


class TestTheChainsItDeclines:
    def test_an_iterator_chain(self, make_project):
        found = in_migration(
            make_project,
            """
            def forwards(apps, schema_editor):
                return list(Book.objects.all().iterator())
            """,
        )
        assert found == []

    def test_a_filtered_queryset(self, make_project):
        """`filter` does not prove the result is small, but it removes the
        rule's evidence that the read covers the whole table."""
        found = in_migration(
            make_project,
            """
            def forwards(apps, schema_editor):
                return list(Book.objects.filter(sold__gt=0))
            """,
        )
        assert found == []

    def test_an_excluded_queryset(self, make_project):
        found = in_migration(
            make_project,
            """
            def forwards(apps, schema_editor):
                return list(Book.objects.exclude(sold=0))
            """,
        )
        assert found == []

    def test_a_sliced_queryset(self, make_project):
        found = in_migration(
            make_project,
            """
            def forwards(apps, schema_editor):
                return list(Book.objects.all()[:100])
            """,
        )
        assert found == []

    def test_values_list_builds_no_instances(self, make_project):
        found = in_migration(
            make_project,
            """
            def forwards(apps, schema_editor):
                return list(Book.objects.values_list("pk", flat=True))
            """,
        )
        assert found == []

    def test_a_terminal_chain(self, make_project):
        """The chain still roots at `Book.objects` and is still tracked, but
        `first()` has left queryset-land -- there is no table here to stream.
        Verified by hand that the tracker really does return a `Book` value
        with `terminal` set, because an earlier version of this test used a
        shape the tracker declined outright and so proved nothing."""
        found = in_migration(
            make_project,
            """
            def forwards(apps, schema_editor):
                return list(Book.objects.all().first())
            """,
        )
        assert found == []

    def test_a_queryset_whose_model_is_unknown(self, make_project):
        """`self.get_queryset()` is recognisably a queryset and its model is
        not knowable, so there is nothing the finding could name."""
        found = findings(
            make_project,
            {
                "library/management/__init__.py": "",
                "library/management/commands/__init__.py": "",
                "library/management/commands/backfill.py": """
                from django.core.management.base import BaseCommand

                from library.models import Book


                class Command(BaseCommand):
                    def get_queryset(self):
                        return Book.objects.all()

                    def handle(self, *args, **options):
                        return list(self.get_queryset())
                """,
            },
        )
        assert found == []

    def test_a_model_the_tracker_cannot_name(self, make_project):
        """The common migration idiom, and the rule's largest recall gap.
        `apps.get_model()` returns a historical model, and the tracker declines
        the expression outright rather than returning a queryset with no model
        -- so this is quiet for a different reason than the unknown-model test
        above. It is why the rule finds nothing in healthchecks or netbox."""
        found = in_migration(
            make_project,
            """
            def forwards(apps, schema_editor):
                Historical = apps.get_model("library", "Book")
                return list(Historical.objects.all())
            """,
        )
        assert found == []


class TestTheShapesItDeclines:
    def test_a_bare_for_loop(self, make_project):
        """It fills the result cache too, but in the corpora these loops are
        overwhelmingly the ones DJP-007 already speaks about."""
        found = in_migration(
            make_project,
            """
            def forwards(apps, schema_editor):
                for book in Book.objects.all():
                    book.title = book.title.strip()
            """,
        )
        assert found == []

    def test_reversed_is_not_a_materialiser(self, make_project):
        """`reversed()` raises on a queryset rather than building a list, so
        reporting it would be reporting a crash as a slow read."""
        found = in_migration(
            make_project,
            """
            def forwards(apps, schema_editor):
                return list(reversed(Book.objects.all()))
            """,
        )
        assert found == []

    def test_a_list_of_something_that_is_not_a_queryset(self, make_project):
        found = in_migration(
            make_project,
            """
            def forwards(apps, schema_editor):
                return list(range(1000))
            """,
        )
        assert found == []

    def test_list_with_a_second_argument(self, make_project):
        found = in_migration(
            make_project,
            """
            def forwards(apps, schema_editor):
                return sorted(Book.objects.all(), key=lambda b: b.title)
            """,
        )
        assert found == []

    def test_an_empty_list_call(self, make_project):
        """`list()` takes no argument, and reading `args[0]` would crash."""
        found = in_migration(
            make_project,
            """
            def forwards(apps, schema_editor):
                rows = list()
                rows.append(1)
                return rows
            """,
        )
        assert found == []

    def test_an_async_comprehension(self, make_project):
        """An async comprehension iterates an async queryset, whose streaming
        behaviour is a different question this rule has not measured."""
        found = in_migration(
            make_project,
            """
            async def forwards(apps, schema_editor):
                return [book.title async for book in Book.objects.all()]
            """,
        )
        assert found == []

    def test_a_project_with_no_production_contexts(self, make_project):
        found = findings(make_project, {"library/empty.py": "x = 1\n"})
        assert found == []

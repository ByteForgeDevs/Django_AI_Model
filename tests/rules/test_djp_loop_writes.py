"""DJP-007 -- a write issued once per row of a loop.

Almost every test here is about a case the rule must decline, because that is
where the rule's correctness lives. `bulk_update` and `bulk_create` are not
drop-in replacements for `save()`: they call no `save()` override, they send no
`pre_save`/`post_save` signal and they do not refresh an `auto_now` column.
Each of those three is measured in `scripts/prefetch_cache_probe.py`, and each
gets a test here proving the rule stays quiet.
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


class Timed(models.Model):
    title = models.CharField(max_length=200)
    changed = models.DateTimeField(auto_now=True)


class Custom(models.Model):
    title = models.CharField(max_length=200)

    def save(self, *args, **kwargs):
        self.title = self.title.strip()
        super().save(*args, **kwargs)


class BaseWithSave(models.Model):
    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)


class Derived(BaseWithSave):
    title = models.CharField(max_length=200)


class Watched(models.Model):
    title = models.CharField(max_length=200)


class Presaved(models.Model):
    title = models.CharField(max_length=200)


class Stamped(models.Model):
    title = models.CharField(max_length=200)
    created = models.DateTimeField(auto_now_add=True)


class TimedBase(models.Model):
    changed = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Inheriting(TimedBase):
    title = models.CharField(max_length=200)
"""

SIGNALS = """
from django.db.models.signals import m2m_changed, post_save, pre_save
from django.dispatch import receiver

from library.models import Book, Presaved, Watched


@receiver(post_save, sender=Watched)
def touched(sender, instance, **kwargs):
    pass


@receiver(pre_save, sender=Presaved)
def before(sender, instance, **kwargs):
    pass


def tagged(sender, instance, **kwargs):
    pass


m2m_changed.connect(tagged, sender=Book)
"""


def findings(make_project, code: str) -> list[Finding]:
    ctx: ProjectContext = make_project(
        {
            "manage.py": "import os\n",
            "library/__init__.py": "",
            "library/settings.py": SETTINGS,
            "library/urls.py": "urlpatterns = []\n",
            "library/models.py": MODELS,
            "library/signals.py": SIGNALS,
            "library/service.py": textwrap.dedent(code).lstrip(),
        }
    )
    rule = next(r for r in all_rules() if r.meta.id == "DJP-007")
    return list(rule().check(ctx))


class TestTheReportedShapes:
    def test_a_plain_save_in_a_loop(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Book

            def restock():
                for book in Book.objects.all():
                    book.sold = 0
                    book.save()
            """,
        )
        assert len(found) == 1
        assert found[0].properties["suggested"] == "bulk_update"
        assert found[0].properties["model"] == "library.Book"
        assert found[0].properties["loop_line"] == "4"

    def test_save_with_update_fields(self, make_project):
        """`update_fields` names exactly what `bulk_update` needs, so it makes
        the rule more confident rather than less."""
        found = findings(
            make_project,
            """
            from library.models import Book

            def restock():
                for book in Book.objects.filter(sold__gt=0):
                    book.sold = 0
                    book.save(update_fields=["sold"])
            """,
        )
        assert len(found) == 1
        assert found[0].properties["suggested"] == "bulk_update"

    def test_a_clone_is_an_insert(self, make_project):
        """`row.pk = None` before a save is how a row is copied, and the
        remediation for a copy is `bulk_create`, not `bulk_update`."""
        found = findings(
            make_project,
            """
            from library.models import Book

            def copy_all():
                for book in Book.objects.all():
                    book.pk = None
                    book.save()
            """,
        )
        assert len(found) == 1
        assert found[0].properties["suggested"] == "bulk_create"

    def test_force_insert_is_an_insert(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Book

            def copy_all():
                for book in Book.objects.all():
                    book.save(force_insert=True)
            """,
        )
        assert found[0].properties["suggested"] == "bulk_create"

    def test_the_evidence_names_the_measurement(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Book

            def restock():
                for book in Book.objects.all():
                    book.save()
            """,
        )
        content = found[0].evidence[0].content
        assert "21 queries this way" in content
        assert "library.Book" in content


class TestWhatIsDeclined:
    def test_a_model_with_a_custom_save(self, make_project):
        """No bulk write calls it, so the advice would silently drop the
        override. The `Book` loop beside it is the contrast: without it this
        would pass even if the rule had stopped working entirely."""
        found = findings(
            make_project,
            """
            from library.models import Book, Custom

            def go():
                for row in Custom.objects.all():
                    row.save()
                for book in Book.objects.all():
                    book.save()
            """,
        )
        assert [f.properties["model"] for f in found] == ["library.Book"]

    def test_a_save_inherited_from_a_base(self, make_project):
        """`Derived` writes no `save()` of its own but inherits one, and
        `bulk_update` bypasses an inherited override just as completely."""
        found = findings(
            make_project,
            """
            from library.models import Derived

            def go():
                for row in Derived.objects.all():
                    row.save()
            """,
        )
        assert found == []

    def test_a_model_with_a_save_signal(self, make_project):
        """Measured: a 20-row loop delivers 20 `post_save` signals and
        `bulk_update` delivers none."""
        found = findings(
            make_project,
            """
            from library.models import Watched

            def go():
                for row in Watched.objects.all():
                    row.save()
            """,
        )
        assert found == []

    def test_a_model_with_an_auto_now_column(self, make_project):
        """Measured: the column advances under `save()` and does not under
        `bulk_update`, so the naive fix stops maintaining a timestamp."""
        found = findings(
            make_project,
            """
            from library.models import Timed

            def go():
                for row in Timed.objects.all():
                    row.save()
            """,
        )
        assert found == []

    def test_a_sliced_loop(self, make_project):
        """A slice caps the row count, which caps the damage."""
        found = findings(
            make_project,
            """
            from library.models import Book

            def go():
                for book in Book.objects.all()[:10]:
                    book.save()
            """,
        )
        assert found == []

    def test_a_loop_over_something_that_is_not_a_queryset(self, make_project):
        """A bulk write needs rows of one model to write back, so a loop whose
        rows cannot be named is not a candidate."""
        found = findings(
            make_project,
            """
            def go(rows):
                for row in rows:
                    row.save()
                for i in range(10):
                    i.save()
            """,
        )
        assert found == []

    def test_a_delete_in_a_loop(self, make_project):
        """There is no `bulk_delete`, and a queryset `delete()` changes which
        cascades and signals run."""
        found = findings(
            make_project,
            """
            from library.models import Book

            def go():
                for book in Book.objects.all():
                    book.delete()
            """,
        )
        assert found == []

    def test_a_save_on_something_other_than_the_loop_target(self, make_project):
        """The row being written has to be the row being iterated, or
        collecting the loop target into a list fixes nothing."""
        found = findings(
            make_project,
            """
            from library.models import Book

            def go(report):
                for book in Book.objects.all():
                    report.total += book.sold
                    report.save()
            """,
        )
        assert found == []

    def test_a_save_outside_the_loop(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Book

            def go():
                last = None
                for book in Book.objects.all():
                    last = book
                if last:
                    last.save()
            """,
        )
        assert found == []

    def test_a_pre_save_receiver_also_blocks(self, make_project):
        """`post_save` is not the only signal a bulk write skips. `pre_save`
        is where a field gets derived from another, so a bulk write silently
        stops deriving it."""
        found = findings(
            make_project,
            """
            from library.models import Presaved

            def go():
                for row in Presaved.objects.all():
                    row.title = row.title.upper()
                    row.save()
            """,
        )
        assert found == []

    def test_a_non_save_signal_does_not_block(self, make_project):
        """`m2m_changed.connect(handler, sender=Book)` names Book as a sender,
        but not of a save. `bulk_update` changes nothing about m2m writes, so
        Book stays reportable -- only `pre_save`/`post_save` are blockers."""
        found = findings(
            make_project,
            """
            from library.models import Book

            def restock():
                for book in Book.objects.all():
                    book.sold = 0
                    book.save()
            """,
        )
        assert len(found) == 1
        assert found[0].properties["model"] == "library.Book"

    def test_auto_now_add_blocks_as_well_as_auto_now(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Stamped

            def go():
                for row in Stamped.objects.all():
                    row.title = "x"
                    row.save()
            """,
        )
        assert found == []

    def test_an_inherited_auto_now_field_blocks(self, make_project):
        """The column is declared on an abstract base, so it reaches the model
        through `inherited` rather than `fields`. It refreshes just the same."""
        found = findings(
            make_project,
            """
            from library.models import Inheriting

            def go():
                for row in Inheriting.objects.all():
                    row.title = "x"
                    row.save()
            """,
        )
        assert found == []

    def test_assigning_id_to_none_is_an_insert(self, make_project):
        """`pk` is the common spelling of the clone idiom, `id` is the other
        one, and both turn the following `save()` into an INSERT."""
        found = findings(
            make_project,
            """
            from library.models import Book

            def clone():
                for book in Book.objects.all():
                    book.id = None
                    book.save()
            """,
        )
        assert len(found) == 1
        assert found[0].properties["suggested"] == "bulk_create"

    def test_another_objects_pk_reset_is_not_the_targets(self, make_project):
        """A `pk = None` somewhere in the body only makes the write an insert
        when it is the written row's own pk."""
        found = findings(
            make_project,
            """
            from library.models import Book

            def restock():
                for book in Book.objects.all():
                    spare = Book(title=book.title)
                    spare.pk = None
                    book.sold = 0
                    book.save()
            """,
        )
        assert len(found) == 1
        assert found[0].properties["suggested"] == "bulk_update"

    def test_a_project_with_no_loops(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Book

            def go():
                return Book.objects.count()
            """,
        )
        assert found == []

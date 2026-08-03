"""DJA -- data exposure rules, and the near-misses they must stay silent on.

Both benchmarks are quiet for DJA-008 and DJA-009: neither NetBox nor pretix
uses `'__all__'` or `exclude` anywhere, which was verified against their source
rather than inferred from a clean report. A rule that cannot fire and a rule
with nothing to find look identical in a report, so recall for this family is
demonstrated here and only here -- every rule is shown firing on a project
built to trigger it and staying silent on the nearest correct spelling.
"""

from __future__ import annotations

from djaudit.context import ProjectContext
from djaudit.models import Finding
from djaudit.registry import all_rules

DRF_SERIALIZERS = """
class Field:
    def __init__(self, *args, **kwargs):
        pass

class CharField(Field):
    pass

class IntegerField(Field):
    pass

class PrimaryKeyRelatedField(Field):
    pass

class SlugRelatedField(Field):
    pass

class HiddenField(Field):
    pass

class CurrentUserDefault:
    pass

class Serializer(Field):
    pass

class ModelSerializer(Serializer):
    pass

class HyperlinkedModelSerializer(ModelSerializer):
    pass
"""

MODELS = """
from django.db import models
from django.conf import settings

class Note(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    title = models.CharField(max_length=50)
    body = models.TextField()

class Tag(models.Model):
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    label = models.CharField(max_length=50)

class Region(models.Model):
    name = models.CharField(max_length=50)
"""

SETTINGS = (
    "SECRET_KEY = 'x'\nDEBUG = False\nALLOWED_HOSTS = ['example.com']\n"
    "INSTALLED_APPS = ['shop']\nROOT_URLCONF = 'shop.urls'\n"
)


def project(make_project, api_source: str, **extra: str) -> ProjectContext:
    built: ProjectContext = make_project(
        {
            "manage.py": "",
            "rest_framework/__init__.py": "",
            "rest_framework/serializers.py": DRF_SERIALIZERS,
            "shop/__init__.py": "",
            "shop/models.py": MODELS,
            "shop/api.py": api_source,
            "shop/urls.py": "urlpatterns = []\n",
            "shop/settings.py": SETTINGS,
            **extra,
        }
    )
    return built


def run(make_project, rule_id: str, api_source: str, **extra: str) -> list[Finding]:
    ctx = project(make_project, api_source, **extra)
    rule = next(r for r in all_rules() if r.meta.id == rule_id)()
    return list(rule.check(ctx))


class TestOpenFieldList:
    """DJA-008 -- `fields = '__all__'`."""

    def test_reports_all_fields(self, make_project) -> None:
        found = run(
            make_project,
            "DJA-008",
            """
            from rest_framework import serializers
            from shop.models import Note

            class NoteSerializer(serializers.ModelSerializer):
                class Meta:
                    model = Note
                    fields = '__all__'
            """,
        )
        assert len(found) == 1
        assert "NoteSerializer" in found[0].message
        assert found[0].location.file == "shop/api.py"

    def test_names_the_columns_it_expands_to(self, make_project) -> None:
        """The argument for fixing it is the column they had forgotten about."""
        found = run(
            make_project,
            "DJA-008",
            """
            from rest_framework import serializers
            from shop.models import Note

            class NoteSerializer(serializers.ModelSerializer):
                class Meta:
                    model = Note
                    fields = '__all__'
            """,
        )
        blob = " ".join(e.content for e in found[0].evidence)
        assert "body" in blob and "title" in blob and "owner" in blob

    def test_silent_on_an_explicit_list(self, make_project) -> None:
        found = run(
            make_project,
            "DJA-008",
            """
            from rest_framework import serializers
            from shop.models import Note

            class NoteSerializer(serializers.ModelSerializer):
                class Meta:
                    model = Note
                    fields = ['id', 'title', 'body']
            """,
        )
        assert found == []

    def test_silent_on_a_plain_serializer(self, make_project) -> None:
        """Without a model there is nothing for `'__all__'` to expand to."""
        found = run(
            make_project,
            "DJA-008",
            """
            from rest_framework import serializers

            class PingSerializer(serializers.Serializer):
                name = serializers.CharField()

                class Meta:
                    fields = '__all__'
            """,
        )
        assert found == []

    def test_silent_on_an_abstract_mixin(self, make_project) -> None:
        """No `Meta` of its own means the subclass decides, so report that instead."""
        found = run(
            make_project,
            "DJA-008",
            """
            from rest_framework import serializers
            from shop.models import Note

            class BaseSerializer(serializers.ModelSerializer):
                pass

            class NoteSerializer(BaseSerializer):
                class Meta:
                    model = Note
                    fields = ['id', 'title']
            """,
        )
        assert found == []

    def test_reports_once_per_serializer_not_once_per_view(self, make_project) -> None:
        found = run(
            make_project,
            "DJA-008",
            """
            from rest_framework import serializers
            from shop.models import Note

            class NoteSerializer(serializers.ModelSerializer):
                class Meta:
                    model = Note
                    fields = '__all__'

            class NoteViewSet:
                serializer_class = NoteSerializer

            class OtherViewSet:
                serializer_class = NoteSerializer
            """,
        )
        assert len(found) == 1

    def test_finds_it_through_an_unreadable_base(self, make_project) -> None:
        """pretix's serializers all inherit from a package we cannot read."""
        found = run(
            make_project,
            "DJA-008",
            """
            from i18nfield.rest_framework import I18nAwareModelSerializer
            from shop.models import Note

            class NoteSerializer(I18nAwareModelSerializer):
                class Meta:
                    model = Note
                    fields = '__all__'
            """,
        )
        assert len(found) == 1


class TestExcludeFieldList:
    """DJA-009 -- `Meta.exclude`, which reads as careful and is not."""

    def test_reports_exclude(self, make_project) -> None:
        found = run(
            make_project,
            "DJA-009",
            """
            from rest_framework import serializers
            from shop.models import Note

            class NoteSerializer(serializers.ModelSerializer):
                class Meta:
                    model = Note
                    exclude = ['body']
            """,
        )
        assert len(found) == 1
        assert "NoteSerializer" in found[0].message

    def test_names_the_hidden_fields(self, make_project) -> None:
        """The denylist is the whole argument, so quote it back."""
        found = run(
            make_project,
            "DJA-009",
            """
            from rest_framework import serializers
            from shop.models import Note

            class NoteSerializer(serializers.ModelSerializer):
                class Meta:
                    model = Note
                    exclude = ['body', 'owner']
            """,
        )
        blob = " ".join(e.content for e in found[0].evidence)
        assert "exclude = [body, owner]" in blob

    def test_silent_on_an_explicit_list(self, make_project) -> None:
        found = run(
            make_project,
            "DJA-009",
            """
            from rest_framework import serializers
            from shop.models import Note

            class NoteSerializer(serializers.ModelSerializer):
                class Meta:
                    model = Note
                    fields = ['id', 'title']
            """,
        )
        assert found == []

    def test_all_fields_is_not_reported_here(self, make_project) -> None:
        """DJA-008 owns that spelling; two findings on one line is one too many."""
        found = run(
            make_project,
            "DJA-009",
            """
            from rest_framework import serializers
            from shop.models import Note

            class NoteSerializer(serializers.ModelSerializer):
                class Meta:
                    model = Note
                    fields = '__all__'
            """,
        )
        assert found == []

    def test_exclude_and_all_fields_do_not_both_report(self, make_project) -> None:
        ids: set[tuple[str, int]] = set()
        for rule_id in ("DJA-008", "DJA-009"):
            ids.update(
                (f.rule_id, f.location.line)
                for f in run(
                    make_project,
                    rule_id,
                    """
                    from rest_framework import serializers
                    from shop.models import Note

                    class OpenSerializer(serializers.ModelSerializer):
                        class Meta:
                            model = Note
                            fields = '__all__'

                    class DenySerializer(serializers.ModelSerializer):
                        class Meta:
                            model = Note
                            exclude = ['body']
                    """,
                )
            )
        assert len(ids) == 2
        assert len({line for _, line in ids}) == 2

    def test_silent_on_an_abstract_mixin(self, make_project) -> None:
        found = run(
            make_project,
            "DJA-009",
            """
            from rest_framework import serializers
            from shop.models import Note

            class BaseSerializer(serializers.ModelSerializer):
                pass

            class NoteSerializer(BaseSerializer):
                class Meta:
                    model = Note
                    fields = ['id', 'title']
            """,
        )
        assert found == []

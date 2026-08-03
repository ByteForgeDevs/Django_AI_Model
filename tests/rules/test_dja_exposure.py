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
from djaudit.models import Finding, Severity
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


SECRET_MODELS = """
from django.db import models
from django.conf import settings

class Webhook(models.Model):
    name = models.CharField(max_length=50)
    secret = models.CharField(max_length=100)
    token = models.CharField(max_length=100)

class Cable(models.Model):
    label = models.CharField(max_length=50)
    is_active = models.BooleanField(default=True)
    groups = models.CharField(max_length=50)
"""


class TestSensitiveField:
    """DJA-010 -- credential material in a response."""

    def test_reports_a_secret_field(self, make_project) -> None:
        found = run(
            make_project,
            "DJA-010",
            """
            from rest_framework import serializers
            from shop.models import Webhook

            class WebhookSerializer(serializers.ModelSerializer):
                class Meta:
                    model = Webhook
                    fields = ['id', 'name', 'secret']
            """,
            **{"shop/models.py": SECRET_MODELS},
        )
        assert len(found) == 1
        assert "'secret'" in found[0].message

    def test_silent_on_write_only(self, make_project) -> None:
        """The correct pattern, and one keyword from the defect."""
        found = run(
            make_project,
            "DJA-010",
            """
            from rest_framework import serializers
            from shop.models import Webhook

            class WebhookSerializer(serializers.ModelSerializer):
                class Meta:
                    model = Webhook
                    fields = ['id', 'name', 'secret']
                    extra_kwargs = {'secret': {'write_only': True}}
            """,
            **{"shop/models.py": SECRET_MODELS},
        )
        assert found == []

    def test_read_only_does_not_excuse_a_secret(self, make_project) -> None:
        """read_only blocks writes and guarantees reads -- the wrong half."""
        found = run(
            make_project,
            "DJA-010",
            """
            from rest_framework import serializers
            from shop.models import Webhook

            class WebhookSerializer(serializers.ModelSerializer):
                class Meta:
                    model = Webhook
                    fields = ['id', 'name', 'secret']
                    read_only_fields = ['secret']
            """,
            **{"shop/models.py": SECRET_MODELS},
        )
        assert len(found) == 1
        assert "read_only" in " ".join(e.content for e in found[0].evidence)

    def test_silent_on_a_field_not_listed(self, make_project) -> None:
        found = run(
            make_project,
            "DJA-010",
            """
            from rest_framework import serializers
            from shop.models import Webhook

            class WebhookSerializer(serializers.ModelSerializer):
                class Meta:
                    model = Webhook
                    fields = ['id', 'name']
            """,
            **{"shop/models.py": SECRET_MODELS},
        )
        assert found == []

    def test_privilege_names_are_ignored_off_the_user_model(self, make_project) -> None:
        """NetBox has `is_active` on a cable path and `groups` on a contact.

        Neither decides anything about a session. A name is not evidence.
        """
        found = run(
            make_project,
            "DJA-010",
            """
            from rest_framework import serializers
            from shop.models import Cable

            class CableSerializer(serializers.ModelSerializer):
                class Meta:
                    model = Cable
                    fields = ['id', 'label', 'is_active', 'groups']
            """,
            **{"shop/models.py": SECRET_MODELS},
        )
        assert found == []

    def test_privilege_names_are_reported_on_the_user_model(self, make_project) -> None:
        found = run(
            make_project,
            "DJA-010",
            """
            from rest_framework import serializers
            from django.contrib.auth.models import User

            class UserSerializer(serializers.ModelSerializer):
                class Meta:
                    model = User
                    fields = ['id', 'username', 'is_staff', 'is_superuser']
            """,
            **{
                "shop/models.py": SECRET_MODELS,
                "shop/settings.py": SETTINGS + "AUTH_USER_MODEL = 'auth.User'\n",
            },
        )
        names = {f.message.split("returns ")[1].split(",")[0] for f in found}
        assert names == {"'is_staff'", "'is_superuser'"}

    def test_a_secret_outranks_a_privilege_flag(self, make_project) -> None:
        """Losing a credential is not the same as learning who has one."""
        found = run(
            make_project,
            "DJA-010",
            """
            from rest_framework import serializers
            from shop.models import Webhook

            class WebhookSerializer(serializers.ModelSerializer):
                class Meta:
                    model = Webhook
                    fields = ['id', 'secret']
            """,
            **{"shop/models.py": SECRET_MODELS},
        )
        assert found[0].severity is Severity.HIGH

    def test_a_repeated_name_reports_once(self, make_project) -> None:
        """NetBox's TokenProvisionSerializer lists `key` twice."""
        found = run(
            make_project,
            "DJA-010",
            """
            from rest_framework import serializers
            from shop.models import Webhook

            class WebhookSerializer(serializers.ModelSerializer):
                class Meta:
                    model = Webhook
                    fields = ['id', 'secret', 'secret']
            """,
            **{"shop/models.py": SECRET_MODELS},
        )
        assert len(found) == 1

    def test_points_at_the_declaration_when_there_is_one(self, make_project) -> None:
        found = run(
            make_project,
            "DJA-010",
            """
            from rest_framework import serializers
            from shop.models import Webhook

            class WebhookSerializer(serializers.ModelSerializer):
                secret = serializers.CharField()

                class Meta:
                    model = Webhook
                    fields = ['id', 'secret']
            """,
            **{"shop/models.py": SECRET_MODELS},
        )
        assert found[0].location.line == 5

    def test_an_inherited_declaration_does_not_borrow_its_line(self, make_project) -> None:
        """A base class's line number belongs to a different file."""
        found = run(
            make_project,
            "DJA-010",
            """
            from rest_framework import serializers
            from shop.models import Webhook

            class BaseSerializer(serializers.ModelSerializer):
                secret = serializers.CharField()

            class WebhookSerializer(BaseSerializer):
                class Meta:
                    model = Webhook
                    fields = ['id', 'secret']
            """,
            **{"shop/models.py": SECRET_MODELS},
        )
        assert found[0].location.line == 7

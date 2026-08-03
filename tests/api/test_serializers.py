"""Reading serializers, which is where a model field becomes public.

The interesting cases are the ones where what a developer wrote and what DRF
emits are not the same list. `fields = "__all__"` and `exclude = [...]` are
both open-ended: the model decides what ships, so a column added later is
published with nobody touching the serializer. `read_only` has three
spellings that a rule must treat alike. And a serializer with no `Meta` of its
own is governed by its parent's, so reading it as "exposes nothing" is a lie.
"""

from __future__ import annotations

import pytest

from djaudit.api import build_api_surface
from djaudit.api.discovery import ApiSurface
from djaudit.api.serializers import ALL_FIELDS
from djaudit.graph.builder import build_model_graph

DRF = """
class Field:
    def __init__(self, *args, **kwargs):
        pass

class CharField(Field):
    pass

class IntegerField(Field):
    pass

class Serializer(Field):
    pass

class ListSerializer(Serializer):
    pass

class ModelSerializer(Serializer):
    pass

class HyperlinkedModelSerializer(ModelSerializer):
    pass
"""

MODELS = """
from django.conf import settings
from django.db import models

class Project(models.Model):
    name = models.CharField(max_length=50)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)

class Check(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE)
    secret = models.CharField(max_length=50)
"""


def surface(make_project, api_source: str, **extra: str) -> ApiSurface:
    """Build a project with a stub DRF on the import path.

    The real DRF is a dev dependency for reading semantics out of, not
    something a fixture can import: djaudit never imports the target, so what
    matters is that the *names* resolve the way an import would.
    """
    files = {
        "rest_framework/__init__.py": "",
        "rest_framework/serializers.py": DRF,
        "shop/__init__.py": "",
        "shop/models.py": MODELS,
        **({"shop/api.py": api_source} if api_source else {}),
        **extra,
    }
    ctx = make_project(files)
    return build_api_surface(ctx, build_model_graph(ctx))


class TestDiscovery:
    def test_a_model_serializer_is_found(self, make_project) -> None:
        found = surface(
            make_project,
            """
            from rest_framework import serializers
            from shop.models import Check

            class CheckSerializer(serializers.ModelSerializer):
                class Meta:
                    model = Check
                    fields = ["id", "project"]
            """,
        )
        node = found.get("shop.api.CheckSerializer")
        assert node is not None
        assert node.is_model_serializer
        assert node.model == "shop.Check"

    def test_a_plain_serializer_is_found_but_not_a_model_one(self, make_project) -> None:
        found = surface(
            make_project,
            """
            from rest_framework import serializers

            class PingSerializer(serializers.Serializer):
                name = serializers.CharField()
            """,
        )
        node = found.get("shop.api.PingSerializer")
        assert node is not None
        assert not node.is_model_serializer
        assert node.model is None

    def test_a_serializer_several_classes_removed_is_still_one(self, make_project) -> None:
        """NetBox's NetBoxModelSerializer is four steps from ModelSerializer and
        mixes in two plain Serializers on the way."""
        found = surface(
            make_project,
            """
            from rest_framework import serializers
            from shop.models import Check

            class ValidatedSerializer(serializers.ModelSerializer):
                pass

            class TaggableMixin(serializers.Serializer):
                pass

            class BaseSerializer(TaggableMixin, ValidatedSerializer):
                pass

            class CheckSerializer(BaseSerializer):
                class Meta:
                    model = Check
                    fields = ["id"]
            """,
        )
        node = found.get("shop.api.CheckSerializer")
        assert node is not None
        assert node.is_model_serializer

    def test_a_class_merely_named_serializer_is_not_one(self, make_project) -> None:
        found = surface(
            make_project,
            """
            class NotASerializer:
                pass
            """,
        )
        assert found.get("shop.api.NotASerializer") is None

    def test_a_model_form_is_not_a_serializer(self, make_project) -> None:
        """The shape is identical -- `class Meta` with `model` and
        `fields = "__all__"` -- and six of NetBox's seven uses of `__all__` are
        forms, not serializers. Only ancestry tells them apart, and a rule that
        confused them would report the wrong construct with the wrong advice.
        """
        found = surface(
            make_project,
            """
            from django import forms
            from shop.models import Check

            class CheckForm(forms.ModelForm):
                class Meta:
                    model = Check
                    fields = "__all__"
            """,
        )
        assert found.get("shop.api.CheckForm") is None
        assert found.for_model("shop.Check") == []

    def test_serializers_are_found_wherever_they_live(self, make_project) -> None:
        """Unlike models, they are not confined to a known module name.
        NetBox spreads 224 of them over api/serializers.py and
        api/serializers_/*.py."""
        found = surface(
            make_project,
            "",
            **{
                "shop/api/__init__.py": "",
                "shop/api/serializers_/__init__.py": "",
                "shop/api/serializers_/checks.py": """
                from rest_framework import serializers
                from shop.models import Check

                class CheckSerializer(serializers.ModelSerializer):
                    class Meta:
                        model = Check
                        fields = ["id"]
                """,
            },
        )
        assert found.get("shop.api.serializers_.checks.CheckSerializer") is not None


class TestFieldSelection:
    def test_an_explicit_list_is_an_allowlist(self, make_project) -> None:
        found = surface(
            make_project,
            """
            from rest_framework import serializers
            from shop.models import Check

            class CheckSerializer(serializers.ModelSerializer):
                class Meta:
                    model = Check
                    fields = ["id", "project"]
            """,
        )
        node = found.get("shop.api.CheckSerializer")
        assert node is not None
        assert node.mode == "explicit"
        assert node.fields == ("id", "project")
        assert not node.is_open_ended

    def test_all_fields_is_open_ended(self, make_project) -> None:
        found = surface(
            make_project,
            """
            from rest_framework import serializers
            from shop.models import Check

            class CheckSerializer(serializers.ModelSerializer):
                class Meta:
                    model = Check
                    fields = "__all__"
            """,
        )
        node = found.get("shop.api.CheckSerializer")
        assert node is not None
        assert node.mode == "all"
        assert node.is_open_ended

    def test_exclude_is_open_ended_too(self, make_project) -> None:
        """A denylist decides only about the columns that existed when it was
        written, which is the whole reason it is worth reporting."""
        found = surface(
            make_project,
            """
            from rest_framework import serializers
            from shop.models import Check

            class CheckSerializer(serializers.ModelSerializer):
                class Meta:
                    model = Check
                    exclude = ["secret"]
            """,
        )
        node = found.get("shop.api.CheckSerializer")
        assert node is not None
        assert node.mode == "exclude"
        assert node.exclude == ("secret",)
        assert node.is_open_ended

    def test_an_unreadable_entry_does_not_discard_the_readable_ones(self, make_project) -> None:
        found = surface(
            make_project,
            """
            from rest_framework import serializers
            from shop.models import Check
            from shop.constants import EXTRA

            class CheckSerializer(serializers.ModelSerializer):
                class Meta:
                    model = Check
                    fields = ["id", EXTRA, "project"]
            """,
            **{"shop/constants.py": "EXTRA = 'secret'"},
        )
        node = found.get("shop.api.CheckSerializer")
        assert node is not None
        assert node.fields == ("id", "project")
        assert "fields" in node.unreadable

    def test_no_meta_means_the_parent_governs(self, make_project) -> None:
        found = surface(
            make_project,
            """
            from rest_framework import serializers
            from shop.models import Check

            class BaseSerializer(serializers.ModelSerializer):
                class Meta:
                    model = Check
                    fields = "__all__"

            class CheckSerializer(BaseSerializer):
                pass
            """,
        )
        node = found.get("shop.api.CheckSerializer")
        assert node is not None
        assert node.meta_inherited
        assert node.mode == "unset"


class TestReadOnly:
    @pytest.mark.parametrize(
        "meta",
        [
            'read_only_fields = ["secret"]',
            'extra_kwargs = {"secret": {"read_only": True}}',
        ],
    )
    def test_meta_spellings(self, make_project, meta: str) -> None:
        found = surface(
            make_project,
            f"""
            from rest_framework import serializers
            from shop.models import Check

            class CheckSerializer(serializers.ModelSerializer):
                class Meta:
                    model = Check
                    fields = ["id", "secret"]
                    {meta}
            """,
        )
        node = found.get("shop.api.CheckSerializer")
        assert node is not None
        assert node.is_read_only("secret")
        assert not node.is_read_only("id")

    def test_a_declared_field_carries_its_own(self, make_project) -> None:
        found = surface(
            make_project,
            """
            from rest_framework import serializers
            from shop.models import Check

            class CheckSerializer(serializers.ModelSerializer):
                secret = serializers.CharField(read_only=True)

                class Meta:
                    model = Check
                    fields = ["id", "secret"]
            """,
        )
        node = found.get("shop.api.CheckSerializer")
        assert node is not None
        assert node.is_read_only("secret")

    def test_write_only_is_not_read_only(self, make_project) -> None:
        found = surface(
            make_project,
            """
            from rest_framework import serializers
            from shop.models import Check

            class CheckSerializer(serializers.ModelSerializer):
                secret = serializers.CharField(write_only=True)

                class Meta:
                    model = Check
                    fields = ["id", "secret"]
            """,
        )
        node = found.get("shop.api.CheckSerializer")
        assert node is not None
        assert node.declared["secret"].write_only
        assert not node.is_read_only("secret")


class TestDeclaredFields:
    def test_a_projects_own_field_class_still_counts(self, make_project) -> None:
        """NetBox's RelatedObjectCountField is a field by any useful measure."""
        found = surface(
            make_project,
            """
            from rest_framework import serializers
            from shop.models import Check
            from shop.fields import RelatedObjectCountField

            class CheckSerializer(serializers.ModelSerializer):
                ping_count = RelatedObjectCountField("pings")

                class Meta:
                    model = Check
                    fields = ["id", "ping_count"]
            """,
            **{"shop/fields.py": "class RelatedObjectCountField:\n    pass\n"},
        )
        node = found.get("shop.api.CheckSerializer")
        assert node is not None
        assert node.declares("ping_count")
        assert node.declared["ping_count"].kind == "RelatedObjectCountField"

    def test_source_is_kept(self, make_project) -> None:
        """The name on the wire and the column behind it differ."""
        found = surface(
            make_project,
            """
            from rest_framework import serializers
            from shop.models import Check

            class CheckSerializer(serializers.ModelSerializer):
                owner_email = serializers.CharField(source="project.owner.email")

                class Meta:
                    model = Check
                    fields = ["id", "owner_email"]
            """,
        )
        node = found.get("shop.api.CheckSerializer")
        assert node is not None
        assert node.declared["owner_email"].source == "project.owner.email"


class TestDepth:
    def test_depth_is_read(self, make_project) -> None:
        found = surface(
            make_project,
            """
            from rest_framework import serializers
            from shop.models import Check

            class CheckSerializer(serializers.ModelSerializer):
                class Meta:
                    model = Check
                    fields = ["id", "project"]
                    depth = 2
            """,
        )
        node = found.get("shop.api.CheckSerializer")
        assert node is not None
        assert node.depth == 2

    def test_it_defaults_to_zero(self, make_project) -> None:
        found = surface(
            make_project,
            """
            from rest_framework import serializers
            from shop.models import Check

            class CheckSerializer(serializers.ModelSerializer):
                class Meta:
                    model = Check
                    fields = ["id"]
            """,
        )
        node = found.get("shop.api.CheckSerializer")
        assert node is not None
        assert node.depth == 0


class TestModelResolution:
    def test_a_dotted_reference_resolves(self, make_project) -> None:
        """`model = models.Check` is how a module-style import spells it."""
        found = surface(
            make_project,
            """
            from rest_framework import serializers
            from shop import models

            class CheckSerializer(serializers.ModelSerializer):
                class Meta:
                    model = models.Check
                    fields = ["id"]
            """,
        )
        node = found.get("shop.api.CheckSerializer")
        assert node is not None
        assert node.model == "shop.Check"

    def test_a_third_party_model_is_recorded_not_guessed(self, make_project) -> None:
        found = surface(
            make_project,
            """
            from rest_framework import serializers
            from taggit.models import TaggedItem

            class TagSerializer(serializers.ModelSerializer):
                class Meta:
                    model = TaggedItem
                    fields = ["id"]
            """,
        )
        node = found.get("shop.api.TagSerializer")
        assert node is not None
        assert node.model is None
        assert "TaggedItem" in found.unresolved_models

    def test_serializers_are_indexed_by_model(self, make_project) -> None:
        """A model exposed twice is exposed by the loosest of the two."""
        found = surface(
            make_project,
            """
            from rest_framework import serializers
            from shop.models import Check

            class CheckSerializer(serializers.ModelSerializer):
                class Meta:
                    model = Check
                    fields = ["id"]

            class CheckDetailSerializer(serializers.ModelSerializer):
                class Meta:
                    model = Check
                    fields = "__all__"
            """,
        )
        both = found.for_model("shop.Check")
        assert len(both) == 2
        assert any(s.is_open_ended for s in both)


class TestAgainstRealDRF:
    def test_all_fields_matches_the_installed_drf(self) -> None:
        """Transcribed constants rot. This one is checked, not trusted."""
        from rest_framework.serializers import ALL_FIELDS as REAL

        assert ALL_FIELDS == REAL

    def test_the_recognised_bases_exist(self) -> None:
        import rest_framework.serializers as drf

        from djaudit.api.serializers import SERIALIZER_BASES

        for path in SERIALIZER_BASES:
            assert hasattr(drf, path.rpartition(".")[2]), path

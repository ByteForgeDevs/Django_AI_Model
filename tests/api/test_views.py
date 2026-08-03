"""Reading views, which is where a serializer becomes an endpoint.

DRF spells a view four ways and they do not share a vocabulary. A generic
answers HTTP methods it never defines a handler for; a viewset answers actions
that are not HTTP methods until a router binds them; an `@api_view` function is
not a class at all. A rule asking "can this be written to" has to get the same
answer from all of them, which is what `writes` is for.

The cases that matter are the inherited ones. NetBox declares two of its twelve
`@action` methods on mixins, and a viewset that mixes one in really does serve
that route -- so reading only a class's own body loses real, writable endpoints
on dozens of viewsets.
"""

from __future__ import annotations

from djaudit.api import build_api_surface
from djaudit.api.discovery import ApiSurface
from djaudit.api.views import ViewNode
from djaudit.graph.builder import build_model_graph

DRF_VIEWS = """
class APIView:
    pass

class GenericAPIView(APIView):
    pass

class ListAPIView(GenericAPIView):
    pass

class ListCreateAPIView(GenericAPIView):
    pass

class RetrieveUpdateDestroyAPIView(GenericAPIView):
    pass
"""

DRF_VIEWSETS = """
class ViewSetMixin:
    pass

class ViewSet(ViewSetMixin):
    pass

class GenericViewSet(ViewSetMixin):
    pass

class ReadOnlyModelViewSet(GenericViewSet):
    pass

class ModelViewSet(GenericViewSet):
    pass
"""

DRF_MIXINS = """
class CreateModelMixin:
    pass

class ListModelMixin:
    pass

class RetrieveModelMixin:
    pass

class UpdateModelMixin:
    pass

class DestroyModelMixin:
    pass
"""

DRF_DECORATORS = """
def api_view(methods=None):
    def wrap(fn):
        return fn
    return wrap

def action(methods=None, detail=None, **kwargs):
    def wrap(fn):
        return fn
    return wrap

def permission_classes(classes):
    def wrap(fn):
        return fn
    return wrap
"""

MODELS = """
from django.conf import settings
from django.db import models

class Project(models.Model):
    name = models.CharField(max_length=50)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
"""


def surface(make_project, api_source: str, **extra: str) -> ApiSurface:
    files = {
        "rest_framework/__init__.py": "",
        "rest_framework/serializers.py": "class ModelSerializer:\n    pass\n",
        "rest_framework/views.py": DRF_VIEWS,
        "rest_framework/generics.py": DRF_VIEWS,
        "rest_framework/viewsets.py": DRF_VIEWSETS,
        "rest_framework/mixins.py": DRF_MIXINS,
        "rest_framework/decorators.py": DRF_DECORATORS,
        "shop/__init__.py": "",
        "shop/models.py": MODELS,
        **({"shop/api.py": api_source} if api_source else {}),
        **extra,
    }
    ctx = make_project(files)
    return build_api_surface(ctx, build_model_graph(ctx))


def ours(found: ApiSurface) -> list[ViewNode]:
    """Only the views the test wrote.

    The stub DRF the fixture puts on the import path is ordinary project
    source as far as discovery is concerned, so ``ListAPIView`` really is a
    class inheriting ``APIView`` in a file we were asked to read. Filtering by
    module keeps that correct behaviour from making every count assertion
    about the stub.
    """
    return [v for v in found.views.values() if v.module.startswith("shop")]


def one(found: ApiSurface, name: str) -> ViewNode:
    (view,) = [v for v in found.views.values() if v.name == name]
    return view


class TestDiscovery:
    def test_an_apiview_is_found_with_the_methods_it_defines(self, make_project) -> None:
        found = surface(
            make_project,
            """
            from rest_framework.views import APIView

            class Ping(APIView):
                def get(self, request):
                    pass

                def post(self, request):
                    pass

                def helper(self):
                    pass
            """,
        )
        view = one(found, "Ping")
        assert view.kind == "apiview"
        assert view.http_methods == ("get", "post")
        assert view.writes

    def test_a_generic_answers_methods_it_never_defines(self, make_project) -> None:
        """The whole point of the transcribed table: no handler is written here."""
        found = surface(
            make_project,
            """
            from rest_framework.generics import ListCreateAPIView

            class Projects(ListCreateAPIView):
                queryset = Project.objects.all()
            """,
        )
        view = one(found, "Projects")
        assert view.kind == "generic"
        assert view.http_methods == ("get", "post")
        assert view.writes

    def test_a_read_only_generic_does_not_write(self, make_project) -> None:
        found = surface(
            make_project,
            """
            from rest_framework.generics import ListAPIView

            class Projects(ListAPIView):
                pass
            """,
        )
        view = one(found, "Projects")
        assert view.http_methods == ("get",)
        assert not view.writes

    def test_a_bare_mixin_is_not_a_view(self, make_project) -> None:
        """Otherwise every base class joins the surface next to the real views."""
        found = surface(
            make_project,
            """
            from rest_framework.mixins import ListModelMixin

            class Fragment(ListModelMixin):
                pass
            """,
        )
        assert not [v for v in ours(found) if v.name == "Fragment"]

    def test_healthchecks_shape_finds_nothing(self, make_project) -> None:
        """A project with no DRF must produce no views rather than an error."""
        found = surface(make_project, "")
        assert not ours(found)


class TestViewsets:
    def test_a_model_viewset_has_all_six_actions(self, make_project) -> None:
        found = surface(
            make_project,
            """
            from rest_framework.viewsets import ModelViewSet

            class ProjectViewSet(ModelViewSet):
                queryset = Project.objects.all()
            """,
        )
        view = one(found, "ProjectViewSet")
        assert view.kind == "viewset"
        assert view.actions == (
            "create",
            "destroy",
            "list",
            "partial_update",
            "retrieve",
            "update",
        )
        assert view.http_methods == ()
        assert view.writes

    def test_a_read_only_viewset_does_not_write(self, make_project) -> None:
        found = surface(
            make_project,
            """
            from rest_framework.viewsets import ReadOnlyModelViewSet

            class ProjectViewSet(ReadOnlyModelViewSet):
                pass
            """,
        )
        view = one(found, "ProjectViewSet")
        assert view.actions == ("list", "retrieve")
        assert not view.writes

    def test_update_mixin_brings_partial_update_too(self, make_project) -> None:
        """PATCH reaches a viewset only through UpdateModelMixin's second action."""
        found = surface(
            make_project,
            """
            from rest_framework import mixins
            from rest_framework.viewsets import GenericViewSet

            class ProjectViewSet(mixins.UpdateModelMixin, GenericViewSet):
                pass
            """,
        )
        assert one(found, "ProjectViewSet").actions == ("partial_update", "update")

    def test_a_hand_written_viewset_action_is_found(self, make_project) -> None:
        found = surface(
            make_project,
            """
            from rest_framework.viewsets import ViewSet

            class ProjectViewSet(ViewSet):
                def list(self, request):
                    pass

                def destroy(self, request, pk=None):
                    pass
            """,
        )
        view = one(found, "ProjectViewSet")
        assert view.actions == ("destroy", "list")
        assert view.writes


class TestExtraActions:
    def test_an_action_records_its_methods_and_detail(self, make_project) -> None:
        found = surface(
            make_project,
            """
            from rest_framework.decorators import action
            from rest_framework.viewsets import ReadOnlyModelViewSet

            class ProjectViewSet(ReadOnlyModelViewSet):
                @action(detail=False, methods=['post'], url_path='bulk-delete')
                def bulk_delete(self, request):
                    pass
            """,
        )
        view = one(found, "ProjectViewSet")
        (extra,) = view.extra_actions
        assert (extra.name, extra.methods, extra.detail) == (
            "bulk_delete",
            ("post",),
            False,
        )
        assert extra.url_path == "bulk-delete"

    def test_an_action_makes_an_otherwise_read_only_viewset_writable(self, make_project) -> None:
        found = surface(
            make_project,
            """
            from rest_framework.decorators import action
            from rest_framework.viewsets import ReadOnlyModelViewSet

            class ProjectViewSet(ReadOnlyModelViewSet):
                @action(detail=True, methods=['post'])
                def sync(self, request, pk=None):
                    pass
            """,
        )
        assert one(found, "ProjectViewSet").writes

    def test_an_action_defaults_to_get(self, make_project) -> None:
        found = surface(
            make_project,
            """
            from rest_framework.decorators import action
            from rest_framework.viewsets import ReadOnlyModelViewSet

            class ProjectViewSet(ReadOnlyModelViewSet):
                @action(detail=True)
                def report(self, request, pk=None):
                    pass
            """,
        )
        (extra,) = one(found, "ProjectViewSet").extra_actions
        assert extra.methods == ("get",)

    def test_an_action_inherited_from_a_mixin_is_a_route_here(self, make_project) -> None:
        """NetBox's SyncedDataMixin shape: the action is declared elsewhere."""
        found = surface(
            make_project,
            """
            from rest_framework.viewsets import ReadOnlyModelViewSet
            from shop.mixins import SyncedDataMixin

            class ProjectViewSet(SyncedDataMixin, ReadOnlyModelViewSet):
                pass
            """,
            **{
                "shop/mixins.py": """
                from rest_framework.decorators import action

                class SyncedDataMixin:
                    @action(detail=True, methods=['post'])
                    def sync(self, request, pk=None):
                        pass
                """
            },
        )
        view = one(found, "ProjectViewSet")
        assert [e.name for e in view.extra_actions] == ["sync"]
        assert view.writes


class TestAttributes:
    def test_a_queryset_survives_being_a_call(self, make_project) -> None:
        """dotted_name cannot render Project.objects.all(); the source can."""
        found = surface(
            make_project,
            """
            from rest_framework.viewsets import ModelViewSet

            class ProjectViewSet(ModelViewSet):
                queryset = Project.objects.prefetch_related('owner')
                serializer_class = ProjectSerializer
            """,
        )
        view = one(found, "ProjectViewSet")
        assert view.queryset_ref == "Project.objects.prefetch_related('owner')"
        assert view.queryset_model_ref == "Project"
        assert view.serializer_ref == "ProjectSerializer"

    def test_attributes_are_inherited_from_a_project_base(self, make_project) -> None:
        found = surface(
            make_project,
            """
            from rest_framework.viewsets import ModelViewSet

            class Base(ModelViewSet):
                queryset = Project.objects.all()
                permission_classes = [IsAuthenticated]

                def get_queryset(self):
                    pass

            class ProjectViewSet(Base):
                pass
            """,
        )
        view = one(found, "ProjectViewSet")
        assert view.queryset_model_ref == "Project"
        assert view.permission_refs == ("IsAuthenticated",)
        assert not view.permissions_unset
        assert "get_queryset" in view.overrides

    def test_the_nearest_declaration_wins(self, make_project) -> None:
        found = surface(
            make_project,
            """
            from rest_framework.viewsets import ModelViewSet

            class Base(ModelViewSet):
                queryset = Project.objects.all()

            class ProjectViewSet(Base):
                queryset = Project.objects.filter(archived=False)
            """,
        )
        assert "archived" in (one(found, "ProjectViewSet").queryset_ref or "")

    def test_no_permission_classes_anywhere_is_recorded(self, make_project) -> None:
        found = surface(
            make_project,
            """
            from rest_framework.viewsets import ModelViewSet

            class ProjectViewSet(ModelViewSet):
                pass
            """,
        )
        view = one(found, "ProjectViewSet")
        assert view.permissions_unset
        assert view.permission_refs == ()


class TestFunctionViews:
    def test_an_api_view_function_is_a_view(self, make_project) -> None:
        found = surface(
            make_project,
            """
            from rest_framework.decorators import api_view

            @api_view(['GET', 'POST'])
            def projects(request):
                pass
            """,
        )
        view = one(found, "projects")
        assert view.kind == "function"
        assert view.http_methods == ("get", "post")
        assert view.writes

    def test_a_bare_api_view_answers_get_only(self, make_project) -> None:
        """DRF's default, and easy to write when POST was meant."""
        found = surface(
            make_project,
            """
            from rest_framework.decorators import api_view

            @api_view()
            def projects(request):
                pass
            """,
        )
        view = one(found, "projects")
        assert view.http_methods == ("get",)
        assert not view.writes

    def test_permission_classes_decorator_is_read(self, make_project) -> None:
        """A function view cannot carry class attributes, so DRF gives it a
        decorator -- and a rule looking only for the attribute finds every
        function view unguarded."""
        found = surface(
            make_project,
            """
            from rest_framework.decorators import api_view, permission_classes

            @api_view(['POST'])
            @permission_classes([IsAdminUser])
            def purge(request):
                pass
            """,
        )
        view = one(found, "purge")
        assert view.permission_refs == ("IsAdminUser",)
        assert not view.permissions_unset

    def test_an_undecorated_function_is_not_a_view(self, make_project) -> None:
        found = surface(
            make_project,
            """
            from rest_framework.decorators import api_view

            def helper(request):
                pass
            """,
        )
        assert not ours(found)

"""Which HTTP methods reach a view, which is not a question the view answers.

A `ModelViewSet` has six actions and no HTTP methods; a router turns the first
into the second, and the mapping it uses decides that DELETE reaches `destroy`
on the detail URL and nothing on the collection URL. Every assertion here is
about that join, because it is where an authorization rule gets the word
"DELETE" from.

The awkward cases are the ones both benchmark projects rely on: a router
subclass that rewrites the standard mapping, and a router instance imported
from another module and registered on from a dozen plugins.
"""

from __future__ import annotations

from djaudit.api import build_api_surface
from djaudit.api.discovery import ApiSurface
from djaudit.api.routes import DETAIL_MAPPING, LIST_MAPPING, Endpoint
from djaudit.graph.builder import build_model_graph

DRF_VIEWS = """
class APIView:
    pass

class GenericAPIView(APIView):
    pass

class ListCreateAPIView(GenericAPIView):
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
"""

DRF_ROUTERS = """
class BaseRouter:
    pass

class SimpleRouter(BaseRouter):
    pass

class DefaultRouter(SimpleRouter):
    pass
"""

MODELS = """
from django.db import models

class Project(models.Model):
    name = models.CharField(max_length=50)
"""


def surface(make_project, api_source: str, urls_source: str, **extra: str) -> ApiSurface:
    files = {
        "rest_framework/__init__.py": "",
        "rest_framework/serializers.py": "class ModelSerializer:\n    pass\n",
        "rest_framework/views.py": DRF_VIEWS,
        "rest_framework/generics.py": DRF_VIEWS,
        "rest_framework/viewsets.py": DRF_VIEWSETS,
        "rest_framework/mixins.py": DRF_MIXINS,
        "rest_framework/decorators.py": DRF_DECORATORS,
        "rest_framework/routers.py": DRF_ROUTERS,
        "shop/__init__.py": "",
        "shop/models.py": MODELS,
        "shop/api.py": api_source,
        "shop/urls.py": urls_source,
        **extra,
    }
    ctx = make_project(files)
    return build_api_surface(ctx, build_model_graph(ctx))


def binding(endpoints: list[Endpoint], kind: str) -> dict[str, str | None]:
    return {e.method: e.action for e in endpoints if e.kind == kind}


MODEL_VIEWSET = """
from rest_framework import viewsets

class ProjectViewSet(viewsets.ModelViewSet):
    queryset = 1
"""

ROUTED = """
from rest_framework import routers
from shop.api import ProjectViewSet

router = routers.DefaultRouter()
router.register('projects', ProjectViewSet)
"""


class TestRouterMappings:
    def test_a_model_viewset_answers_the_full_table(self, make_project):
        found = surface(make_project, MODEL_VIEWSET, ROUTED)
        endpoints = found.routes.for_view("shop.api.ProjectViewSet")
        assert binding(endpoints, "list") == LIST_MAPPING
        assert binding(endpoints, "detail") == DETAIL_MAPPING

    def test_destroy_is_reachable_on_the_detail_url_only(self, make_project):
        """The distinction the whole join exists to make."""
        found = surface(make_project, MODEL_VIEWSET, ROUTED)
        deletes = [e for e in found.routes.for_view("shop.api.ProjectViewSet") if e.writes]
        assert [e.detail for e in deletes if e.method == "delete"] == [True]

    def test_a_read_only_viewset_is_not_credited_with_writes(self, make_project):
        """DRF's ``get_method_map`` binds a method only if the action exists,
        which is why a read-only viewset 405s POST with nobody writing a rule."""
        found = surface(
            make_project,
            """
            from rest_framework import viewsets

            class ProjectViewSet(viewsets.ReadOnlyModelViewSet):
                queryset = 1
            """,
            ROUTED,
        )
        endpoints = found.routes.for_view("shop.api.ProjectViewSet")
        assert {e.method for e in endpoints} == {"get"}
        assert not found.routes.writable

    def test_a_partial_viewset_gets_only_the_actions_it_mixes_in(self, make_project):
        found = surface(
            make_project,
            """
            from rest_framework import mixins, viewsets

            class ProjectViewSet(mixins.ListModelMixin,
                                 mixins.CreateModelMixin,
                                 viewsets.GenericViewSet):
                queryset = 1
            """,
            ROUTED,
        )
        endpoints = found.routes.for_view("shop.api.ProjectViewSet")
        assert binding(endpoints, "list") == {"get": "list", "post": "create"}
        assert binding(endpoints, "detail") == {}

    def test_patch_arrives_through_update_model_mixin(self, make_project):
        """``UpdateModelMixin`` supplies ``partial_update`` as well as
        ``update``, and it is the only route PATCH has into a viewset."""
        found = surface(
            make_project,
            """
            from rest_framework import mixins, viewsets

            class ProjectViewSet(mixins.UpdateModelMixin, viewsets.GenericViewSet):
                queryset = 1
            """,
            ROUTED,
        )
        endpoints = found.routes.for_view("shop.api.ProjectViewSet")
        assert binding(endpoints, "detail") == {"put": "update", "patch": "partial_update"}


class TestCustomRouters:
    def test_a_router_subclass_inherits_the_standard_table(self, make_project):
        found = surface(
            make_project,
            MODEL_VIEWSET,
            """
            from shop.routers import ShopRouter
            from shop.api import ProjectViewSet

            router = ShopRouter()
            router.register('projects', ProjectViewSet)
            """,
            **{
                "shop/routers.py": (
                    "from rest_framework.routers import DefaultRouter\n\n"
                    "class ShopRouter(DefaultRouter):\n    pass\n"
                )
            },
        )
        endpoints = found.routes.for_view("shop.api.ProjectViewSet")
        assert binding(endpoints, "list") == LIST_MAPPING
        assert found.routes.registrations[0].router == "shop.routers.ShopRouter"

    def test_a_rewritten_list_mapping_is_read(self, make_project):
        """NetBox puts bulk PUT, PATCH and DELETE on every collection URL in
        the product by mutating ``self.routes[0].mapping`` in ``__init__``.
        Ignoring it understates 139 endpoints as read-only-plus-POST."""
        found = surface(
            make_project,
            """
            from rest_framework import viewsets

            class ProjectViewSet(viewsets.ModelViewSet):
                queryset = 1

                def bulk_destroy(self, request):
                    pass
            """,
            """
            from shop.routers import ShopRouter
            from shop.api import ProjectViewSet

            router = ShopRouter()
            router.register('projects', ProjectViewSet)
            """,
            **{
                "shop/routers.py": (
                    "from rest_framework.routers import DefaultRouter\n\n"
                    "class ShopRouter(DefaultRouter):\n"
                    "    def __init__(self, *args, **kwargs):\n"
                    "        super().__init__(*args, **kwargs)\n"
                    "        self.routes[0].mapping.update({\n"
                    "            'delete': 'bulk_destroy',\n"
                    "        })\n"
                )
            },
        )
        endpoints = found.routes.for_view("shop.api.ProjectViewSet")
        assert binding(endpoints, "list")["delete"] == "bulk_destroy"

    def test_a_rewritten_mapping_still_requires_the_method_to_exist(self, make_project):
        """The viewset here never defines ``bulk_destroy``, so DRF binds
        nothing and neither may we."""
        found = surface(
            make_project,
            MODEL_VIEWSET,
            """
            from shop.routers import ShopRouter
            from shop.api import ProjectViewSet

            router = ShopRouter()
            router.register('projects', ProjectViewSet)
            """,
            **{
                "shop/routers.py": (
                    "from rest_framework.routers import DefaultRouter\n\n"
                    "class ShopRouter(DefaultRouter):\n"
                    "    def __init__(self):\n"
                    "        self.routes[0].mapping.update({'delete': 'bulk_destroy'})\n"
                )
            },
        )
        endpoints = found.routes.for_view("shop.api.ProjectViewSet")
        assert "delete" not in binding(endpoints, "list")


class TestRegistration:
    def test_a_router_imported_from_elsewhere_is_still_a_router(self, make_project):
        """Every pretix plugin registers on a shared ``event_router`` imported
        from ``pretix.api.urls``. Nine viewsets hang on recognising it."""
        found = surface(
            make_project,
            MODEL_VIEWSET,
            "from rest_framework import routers\n\nshared = routers.DefaultRouter()\n",
            **{
                "plugin/__init__.py": "",
                "plugin/urls.py": (
                    "from shop.urls import shared\n"
                    "from shop.api import ProjectViewSet\n\n"
                    "shared.register('projects', ProjectViewSet)\n"
                ),
            },
        )
        assert len(found.routes.registrations) == 1
        assert found.routes.for_view("shop.api.ProjectViewSet")

    def test_a_registry_that_is_not_a_router_is_ignored(self, make_project):
        """``register`` is a popular method name; pretix has several registries
        of its own and Django's admin has one on every project."""
        found = surface(
            make_project,
            MODEL_VIEWSET,
            "from shop.api import ProjectViewSet\n\n"
            "class Registry:\n    def register(self, a, b):\n        pass\n\n"
            "plugins = Registry()\n"
            "plugins.register('projects', ProjectViewSet)\n",
        )
        assert not found.routes.registrations

    def test_the_admin_is_not_mistaken_for_a_router(self, make_project):
        found = surface(
            make_project,
            MODEL_VIEWSET,
            "from django.contrib import admin\nfrom shop.models import Project\n\n"
            "admin.site.register(Project, admin.ModelAdmin)\n",
        )
        assert not found.routes.registrations

    def test_a_viewset_outside_the_project_is_counted_not_dropped(self, make_project):
        found = surface(
            make_project,
            MODEL_VIEWSET,
            "from rest_framework import routers\n"
            "from vendor.api import VendorViewSet\n\n"
            "router = routers.DefaultRouter()\n"
            "router.register('vendor', VendorViewSet)\n",
        )
        assert found.routes.unresolved_views == ("VendorViewSet",)
        assert not found.routes.endpoints

    def test_the_basename_is_read(self, make_project):
        found = surface(
            make_project,
            MODEL_VIEWSET,
            "from rest_framework import routers\nfrom shop.api import ProjectViewSet\n\n"
            "router = routers.DefaultRouter()\n"
            "router.register('projects', ProjectViewSet, basename='project')\n",
        )
        assert found.routes.registrations[0].basename == "project"

    def test_a_positional_basename_is_read(self, make_project):
        found = surface(
            make_project,
            MODEL_VIEWSET,
            "from rest_framework import routers\nfrom shop.api import ProjectViewSet\n\n"
            "router = routers.DefaultRouter()\n"
            "router.register('projects', ProjectViewSet, 'project')\n",
        )
        assert found.routes.registrations[0].basename == "project"


class TestExtraActions:
    def test_an_action_becomes_its_own_endpoint(self, make_project):
        found = surface(
            make_project,
            """
            from rest_framework import viewsets
            from rest_framework.decorators import action

            class ProjectViewSet(viewsets.ReadOnlyModelViewSet):
                queryset = 1

                @action(detail=False, methods=['post'])
                def archive(self, request):
                    pass
            """,
            ROUTED,
        )
        extra = [e for e in found.routes.for_view("shop.api.ProjectViewSet") if e.kind == "action"]
        assert [(e.method, e.action, e.detail) for e in extra] == [("post", "archive", False)]
        assert extra[0].pattern == "projects/archive"

    def test_url_path_replaces_the_method_name(self, make_project):
        found = surface(
            make_project,
            """
            from rest_framework import viewsets
            from rest_framework.decorators import action

            class ProjectViewSet(viewsets.ReadOnlyModelViewSet):
                queryset = 1

                @action(detail=True, url_path='render-config')
                def render_config(self, request):
                    pass
            """,
            ROUTED,
        )
        extra = [e for e in found.routes.for_view("shop.api.ProjectViewSet") if e.kind == "action"]
        assert extra[0].pattern == "projects/render-config"
        assert extra[0].detail is True

    def test_a_write_action_on_a_read_only_viewset_is_visible(self, make_project):
        """The shape worth finding: the viewset is read-only, so a rule that
        stopped at the standard actions would call the endpoint safe."""
        found = surface(
            make_project,
            """
            from rest_framework import viewsets
            from rest_framework.decorators import action

            class ProjectViewSet(viewsets.ReadOnlyModelViewSet):
                queryset = 1

                @action(detail=False, methods=['post'])
                def purge(self, request):
                    pass
            """,
            ROUTED,
        )
        assert [e.method for e in found.routes.writable] == ["post"]


class TestUrlconfRoutes:
    def test_an_apiview_is_routed_by_as_view(self, make_project):
        found = surface(
            make_project,
            """
            from rest_framework.views import APIView

            class HealthView(APIView):
                def get(self, request):
                    pass

                def post(self, request):
                    pass
            """,
            "from django.urls import path\nfrom shop.api import HealthView\n\n"
            "urlpatterns = [path('health/', HealthView.as_view())]\n",
        )
        endpoints = found.routes.for_view("shop.api.HealthView")
        assert {e.method for e in endpoints} == {"get", "post"}
        assert {e.kind for e in endpoints} == {"path"}
        assert endpoints[0].pattern == "health/"

    def test_a_generic_answers_methods_it_never_defines(self, make_project):
        found = surface(
            make_project,
            "from rest_framework.generics import ListCreateAPIView\n\n"
            "class ProjectList(ListCreateAPIView):\n    queryset = 1\n",
            "from django.urls import path\nfrom shop.api import ProjectList\n\n"
            "urlpatterns = [path('projects/', ProjectList.as_view())]\n",
        )
        assert {e.method for e in found.routes.for_view("shop.api.ProjectList")} == {"get", "post"}

    def test_an_explicit_as_view_mapping_replaces_the_router_table(self, make_project):
        found = surface(
            make_project,
            MODEL_VIEWSET,
            "from django.urls import path\nfrom shop.api import ProjectViewSet\n\n"
            "urlpatterns = [path('projects/', ProjectViewSet.as_view({'get': 'list'}))]\n",
        )
        endpoints = found.routes.for_view("shop.api.ProjectViewSet")
        assert [(e.method, e.action) for e in endpoints] == [("get", "list")]

    def test_a_function_view_is_routed_by_name(self, make_project):
        found = surface(
            make_project,
            "from rest_framework.decorators import api_view\n\n"
            "@api_view(['POST'])\n"
            "def submit(request):\n    pass\n",
            "from django.urls import path\nfrom shop.api import submit\n\n"
            "urlpatterns = [path('submit/', submit)]\n",
        )
        assert [e.method for e in found.routes.for_view("shop.api.submit")] == ["post"]

    def test_a_non_literal_pattern_says_so_rather_than_guessing(self, make_project):
        found = surface(
            make_project,
            "from rest_framework.views import APIView\n\n"
            "class HealthView(APIView):\n    def get(self, request):\n        pass\n",
            "from django.urls import path\nfrom django.conf import settings\n"
            "from shop.api import HealthView\n\n"
            "urlpatterns = [path(f'{settings.PREFIX}health/', HealthView.as_view())]\n",
        )
        endpoint = found.routes.for_view("shop.api.HealthView")[0]
        assert endpoint.pattern == "health/"
        assert not endpoint.literal

    def test_include_is_not_a_view(self, make_project):
        found = surface(
            make_project,
            MODEL_VIEWSET,
            "from django.urls import include, path\n\n"
            "urlpatterns = [path('api/', include('shop.api'))]\n",
        )
        assert not found.routes.endpoints


class TestSurface:
    def test_a_base_class_nothing_registers_is_reported_unrouted(self, make_project):
        """pretix's ``ScheduledExportersViewSet`` is a ``ModelViewSet`` that is
        only ever subclassed. An unreachable view cannot be a defect."""
        found = surface(
            make_project,
            """
            from rest_framework import viewsets

            class BaseProjectViewSet(viewsets.ModelViewSet):
                queryset = 1

            class ProjectViewSet(BaseProjectViewSet):
                pass
            """,
            ROUTED,
        )
        # The stub DRF the fixture puts on the import path is ordinary project
        # source to discovery, so its own base classes are unrouted too.
        ours = [v.name for v in found.unrouted_views if v.module.startswith("shop")]
        assert ours == ["BaseProjectViewSet"]

    def test_methods_for_collapses_both_urls(self, make_project):
        found = surface(make_project, MODEL_VIEWSET, ROUTED)
        assert found.routes.methods_for("shop.api.ProjectViewSet") == (
            "delete",
            "get",
            "patch",
            "post",
            "put",
        )

    def test_a_project_without_drf_has_an_empty_graph(self, make_project):
        found = surface(make_project, "", "")
        assert not found.routes.endpoints
        assert not found.routes.registrations

"""Which rows an endpoint reaches, and whether the request narrows them.

The point of every test here is that "scoped to the requesting user" is a
phrase real projects do not write. NetBox narrows with
``self.queryset.restrict(request.user, action)`` inside a base viewset's
``initial()``; pretix narrows with
``filter(order__event__organizer=self.request.organizer)`` after four
intervening statements, or through a ``@cached_property`` that reads
``self.kwargs``. A pass that looked in ``get_queryset`` for ``request.user``
would find none of it and report every view in both projects.

So these tests are mostly about indirection: locals, ``self`` attributes,
``super()``, base-class hooks, and the two shapes that look unscoped and are
not -- ``.none()`` and a request-scoped ``get_object``.
"""

from __future__ import annotations

import ast

from djaudit.api import build_api_surface
from djaudit.api.querysets import QuerysetNode, Source, call_chain, read_queryset, request_refs
from djaudit.context import ProjectContext
from djaudit.graph.builder import build_model_graph
from djaudit.graph.inheritance import ClassIndex

DRF_VIEWS = """
class APIView:
    pass

class GenericAPIView(APIView):
    pass

class RetrieveUpdateDestroyAPIView(GenericAPIView):
    pass
"""

DRF_VIEWSETS = """
class ViewSetMixin:
    pass

class GenericViewSet(ViewSetMixin):
    pass

class ModelViewSet(GenericViewSet):
    pass
"""

MODELS = """
from django.db import models

class Project(models.Model):
    name = models.CharField(max_length=50)

class Task(models.Model):
    name = models.CharField(max_length=50)
"""

ROUTED = """
from rest_framework import routers
from shop.api import ProjectViewSet

router = routers.DefaultRouter()
router.register('projects', ProjectViewSet)
"""


def build(make_project, api_source: str, **extra: str) -> ProjectContext:
    files = {
        "manage.py": "",
        "rest_framework/__init__.py": "",
        "rest_framework/views.py": DRF_VIEWS,
        "rest_framework/generics.py": DRF_VIEWS,
        "rest_framework/viewsets.py": DRF_VIEWSETS,
        "rest_framework/serializers.py": "class ModelSerializer:\n    pass\n",
        "rest_framework/routers.py": (
            "class BaseRouter:\n    pass\n\n"
            "class SimpleRouter(BaseRouter):\n    pass\n\n"
            "class DefaultRouter(SimpleRouter):\n    pass\n"
        ),
        "shop/__init__.py": "",
        "shop/models.py": MODELS,
        "shop/api.py": api_source,
        "shop/urls.py": ROUTED,
        **extra,
    }
    built: ProjectContext = make_project(files)
    return built


def node_for(make_project, api_source: str, name: str = "ProjectViewSet", **extra: str):
    ctx = build(make_project, api_source, **extra)
    index = ClassIndex(ctx)
    graph = build_model_graph(ctx)
    surface = build_api_surface(ctx, graph, index)
    by_class = {(m.path, m.name): m.label for m in graph}
    return read_queryset(surface.views[f"shop.api.{name}"], index, by_class)


class TestDeclaredQuerysets:
    def test_class_attribute_is_read_with_its_model(self, make_project):
        node = node_for(
            make_project,
            """
            from rest_framework import viewsets
            from shop.models import Project

            class ProjectViewSet(viewsets.ModelViewSet):
                queryset = Project.objects.all()
            """,
        )
        assert node.source is Source.ATTRIBUTE
        assert node.model_ref == "Project"
        assert node.model == "shop.Project"
        assert node.calls == ("objects", "all")
        assert node.unfiltered

    def test_a_view_with_no_queryset_is_absent_not_unfiltered(self, make_project):
        """A plain ``APIView`` has no queryset by design, and reporting one as
        exposing every row would be a fabricated finding."""
        node = node_for(
            make_project,
            """
            from rest_framework.views import APIView

            class ProjectViewSet(APIView):
                def get(self, request):
                    return None
            """,
        )
        assert node.source is Source.ABSENT
        assert not node.unfiltered

    def test_get_queryset_beats_a_nearer_attribute(self, make_project):
        """DRF only ever calls ``get_queryset``; ``GenericAPIView`` reads the
        attribute from inside it, so an override replaces it rather than
        refining it."""
        node = node_for(
            make_project,
            """
            from rest_framework import viewsets
            from shop.models import Project

            class ProjectViewSet(viewsets.ModelViewSet):
                queryset = Project.objects.all()

                def get_queryset(self):
                    return Project.objects.filter(owner=self.request.user)
            """,
        )
        assert node.source is Source.METHOD
        assert node.scoped
        assert not node.unfiltered

    def test_attribute_is_inherited_from_a_project_base(self, make_project):
        node = node_for(
            make_project,
            """
            from rest_framework import viewsets
            from shop.models import Project

            class BaseViewSet(viewsets.ModelViewSet):
                queryset = Project.objects.all()

            class ProjectViewSet(BaseViewSet):
                pass
            """,
        )
        assert node.declared_by == "shop.api.BaseViewSet"
        assert node.model == "shop.Project"


class TestRequestScoping:
    def test_filter_on_request_user_is_scoped(self, make_project):
        node = node_for(
            make_project,
            """
            from rest_framework import viewsets
            from shop.models import Project

            class ProjectViewSet(viewsets.ModelViewSet):
                def get_queryset(self):
                    return Project.objects.filter(owner=self.request.user)
            """,
        )
        assert node.scoped
        assert node.returns[0].scoping == ("self.request.user",)

    def test_a_bare_request_parameter_counts(self, make_project):
        node = node_for(
            make_project,
            """
            from rest_framework import viewsets
            from shop.models import Project

            class ProjectViewSet(viewsets.ModelViewSet):
                def get_queryset(self):
                    request = self.request
                    return Project.objects.filter(owner=request.user)
            """,
        )
        assert node.scoped

    def test_a_url_capture_counts_as_request_input(self, make_project):
        node = node_for(
            make_project,
            """
            from rest_framework import viewsets
            from shop.models import Task

            class ProjectViewSet(viewsets.ModelViewSet):
                def get_queryset(self):
                    return Task.objects.filter(project=self.kwargs['project'])
            """,
        )
        assert node.scoped

    def test_scoping_through_a_local_variable(self, make_project):
        """pretix's dominant shape: build in a local, refine it, return the
        name. Reading only the return expression finds a bare name and
        concludes the endpoint reads every row."""
        node = node_for(
            make_project,
            """
            from rest_framework import viewsets
            from shop.models import Project

            class ProjectViewSet(viewsets.ModelViewSet):
                def get_queryset(self):
                    qs = Project.objects.filter(organizer=self.request.organizer)
                    if self.request.query_params.get('expand'):
                        qs = qs.prefetch_related('tasks')
                    return qs
            """,
        )
        assert node.scoped
        assert node.model == "shop.Project"

    def test_a_self_referential_refinement_terminates(self, make_project):
        node = node_for(
            make_project,
            """
            from rest_framework import viewsets
            from shop.models import Project

            class ProjectViewSet(viewsets.ModelViewSet):
                def get_queryset(self):
                    qs = Project.objects.all()
                    qs = qs.filter(a=1)
                    qs = qs.filter(b=2)
                    return qs
            """,
        )
        assert not node.scoped
        assert node.model == "shop.Project"

    def test_scoping_through_a_cached_property(self, make_project):
        """pretix's ``ItemVariationViewSet`` resolves ``self.item`` in a
        ``@cached_property`` off ``self.kwargs`` and returns
        ``self.item.variations.all()`` -- nothing in the return expression
        shows it is scoped."""
        node = node_for(
            make_project,
            """
            from functools import cached_property

            from rest_framework import viewsets
            from shop.models import Project

            class ProjectViewSet(viewsets.ModelViewSet):
                @cached_property
                def project(self):
                    return Project.objects.get(pk=self.kwargs['project'])

                def get_queryset(self):
                    return self.project.tasks.all()
            """,
        )
        assert node.scoped
        assert not node.unfiltered

    def test_an_unscoped_property_does_not_launder_the_queryset(self, make_project):
        node = node_for(
            make_project,
            """
            from rest_framework import viewsets
            from shop.models import Project

            class ProjectViewSet(viewsets.ModelViewSet):
                @property
                def project(self):
                    return Project.objects.first()

                def get_queryset(self):
                    return self.project.tasks.all()
            """,
        )
        assert not node.scoped
        assert node.unfiltered

    def test_arguments_to_a_call_do_not_root_the_expression(self, make_project):
        """``lookup(request.user).all()`` is rooted at ``lookup``, not at the
        request -- but the reference is still there and still counts."""
        node = node_for(
            make_project,
            """
            from rest_framework import viewsets
            from shop.models import Project

            class ProjectViewSet(viewsets.ModelViewSet):
                def get_queryset(self):
                    return Project.objects.filter(pk=lookup(self.request.user))
            """,
        )
        assert node.scoped
        assert node.model_ref == "Project"


class TestConditionalPaths:
    def test_a_return_guarded_by_a_request_check_is_scoped(self, make_project):
        """pretix's ``OrganizerViewSet`` returns every row -- but only inside
        ``if self.request.user.has_active_staff_session(...)``."""
        node = node_for(
            make_project,
            """
            from rest_framework import viewsets
            from shop.models import Project

            class ProjectViewSet(viewsets.ModelViewSet):
                def get_queryset(self):
                    if self.request.user.is_staff:
                        return Project.objects.all()
                    return Project.objects.filter(owner=self.request.user)
            """,
        )
        assert node.scoped
        assert node.returns[0].guarded
        assert not node.returns[0].scoping

    def test_an_unguarded_unscoped_path_loses_the_verdict(self, make_project):
        node = node_for(
            make_project,
            """
            from rest_framework import viewsets
            from shop.models import Project

            class ProjectViewSet(viewsets.ModelViewSet):
                def get_queryset(self):
                    if self.kwargs.get('all'):
                        return Project.objects.filter(owner=self.request.user)
                    return Project.objects.all()
            """,
        )
        assert not node.scoped
        assert node.unfiltered

    def test_an_else_branch_is_not_inside_the_condition(self, make_project):
        node = node_for(
            make_project,
            """
            from rest_framework import viewsets
            from shop.models import Project

            class ProjectViewSet(viewsets.ModelViewSet):
                def get_queryset(self):
                    if self.request.user.is_staff:
                        return Project.objects.filter(owner=self.request.user)
                    else:
                        return Project.objects.all()
            """,
        )
        assert not node.scoped


class TestAncestryHooks:
    def test_a_base_class_rebinding_self_queryset_scopes_the_subclass(self, make_project):
        """NetBox's whole object-permission scheme. Without reading it, every
        one of its 151 viewsets looks like an unfiltered ``.objects.all()``."""
        node = node_for(
            make_project,
            """
            from rest_framework import viewsets
            from shop.models import Project

            class BaseViewSet(viewsets.ModelViewSet):
                def initial(self, request, *args, **kwargs):
                    super().initial(request, *args, **kwargs)
                    self.queryset = self.queryset.restrict(request.user, 'view')

            class ProjectViewSet(BaseViewSet):
                queryset = Project.objects.all()
            """,
        )
        assert node.scoped
        assert node.rebound_by == ("shop.api.BaseViewSet.initial",)
        assert not node.unfiltered
        assert node.model == "shop.Project"

    def test_a_rebind_with_no_request_reference_does_not_count(self, make_project):
        node = node_for(
            make_project,
            """
            from rest_framework import viewsets
            from shop.models import Project

            class BaseViewSet(viewsets.ModelViewSet):
                def initial(self, request, *args, **kwargs):
                    self.queryset = self.queryset.select_related('owner')

            class ProjectViewSet(BaseViewSet):
                queryset = Project.objects.all()
            """,
        )
        assert not node.scoped
        assert node.rebound_by == ()

    def test_super_get_queryset_is_recorded_as_delegating(self, make_project):
        node = node_for(
            make_project,
            """
            from rest_framework import viewsets
            from shop.models import Project

            class BaseViewSet(viewsets.ModelViewSet):
                def get_queryset(self):
                    return Project.objects.filter(owner=self.request.user)

            class ProjectViewSet(BaseViewSet):
                def get_queryset(self):
                    qs = super().get_queryset()
                    return qs.order_by('name')
            """,
        )
        assert node.returns[0].delegates
        assert node.declared_by == "shop.api.ProjectViewSet.get_queryset"

    def test_get_object_scoping_is_kept_apart_from_the_queryset(self, make_project):
        """NetBox's ``DashboardView``: ``queryset`` is every dashboard in the
        install and ``get_object`` returns only the caller's. That protects a
        detail route completely and a list route not at all, so the two must
        not be folded together."""
        node = node_for(
            make_project,
            """
            from rest_framework.generics import RetrieveUpdateDestroyAPIView
            from shop.models import Project

            class ProjectViewSet(RetrieveUpdateDestroyAPIView):
                queryset = Project.objects.all()

                def get_object(self):
                    return Project.objects.filter(owner=self.request.user).first()
            """,
        )
        assert node.object_scoped
        assert node.object_scoping == ("self.request.user",)
        assert not node.scoped
        assert node.unfiltered


class TestEmptyQuerysets:
    def test_none_is_the_strongest_narrowing_there_is(self, make_project):
        """pretix writes ``queryset = Event.objects.none()`` on views that
        exist only to accept a POST. Reading it as unscoped would report the
        safest shape in the project."""
        node = node_for(
            make_project,
            """
            from rest_framework import viewsets
            from shop.models import Project

            class ProjectViewSet(viewsets.ModelViewSet):
                queryset = Project.objects.none()
            """,
        )
        assert node.empty
        assert not node.unfiltered

    def test_a_get_queryset_returning_none_is_also_empty(self, make_project):
        node = node_for(
            make_project,
            """
            from rest_framework import viewsets
            from shop.models import Project

            class ProjectViewSet(viewsets.ModelViewSet):
                def get_queryset(self):
                    return Project.objects.none()
            """,
        )
        assert node.empty
        assert not node.unfiltered

    def test_none_on_the_attribute_does_not_excuse_an_override(self, make_project):
        node = node_for(
            make_project,
            """
            from rest_framework import viewsets
            from shop.models import Project

            class ProjectViewSet(viewsets.ModelViewSet):
                queryset = Project.objects.none()

                def get_queryset(self):
                    return Project.objects.all()
            """,
        )
        assert not node.empty
        assert node.unfiltered


class TestHelpers:
    def test_call_chain_reads_left_to_right(self):
        expr = ast.parse("Project.objects.filter(x=1).select_related('y')", mode="eval").body
        assert call_chain(expr) == ("objects", "filter", "select_related")

    def test_request_refs_reports_the_longest_expression_once(self):
        expr = ast.parse("Project.objects.filter(a=self.request.user.pk)", mode="eval").body
        assert request_refs(expr) == ("self.request.user.pk",)

    def test_request_refs_ignores_unrelated_attributes(self):
        expr = ast.parse("Project.objects.filter(a=self.default_owner)", mode="eval").body
        assert request_refs(expr) == ()


class TestSurfaceIntegration:
    def test_the_surface_exposes_unfiltered_routed_views(self, make_project):
        ctx = build(
            make_project,
            """
            from rest_framework import viewsets
            from shop.models import Project

            class ProjectViewSet(viewsets.ModelViewSet):
                queryset = Project.objects.all()

            class HiddenViewSet(viewsets.ModelViewSet):
                queryset = Project.objects.all()
            """,
        )
        surface = build_api_surface(ctx, build_model_graph(ctx))
        assert [v.name for v in surface.unfiltered_views] == ["ProjectViewSet"]
        assert "shop.api.HiddenViewSet" not in surface.querysets

    def test_an_unresolvable_model_leaves_the_node_intact(self, make_project):
        node = node_for(
            make_project,
            """
            from rest_framework import viewsets
            from other.models import Thing

            class ProjectViewSet(viewsets.ModelViewSet):
                queryset = Thing.objects.all()
            """,
        )
        assert isinstance(node, QuerysetNode)
        assert node.model_ref == "Thing"
        assert node.model is None
        assert node.unfiltered

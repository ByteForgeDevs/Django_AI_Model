"""What actually guards an endpoint, which is never one file's answer.

Every test here needs three things agreeing: what the view declares, what its
ancestry declares, and what ``REST_FRAMEWORK`` sets -- because DRF's own
default is ``AllowAny`` and so a missing declaration is not a neutral fact.

The two shapes that decide whether this pass is useful at all are both taken
from the benchmarks. A permission class that overrides ``has_permission`` only
to deny more (NetBox's ``TokenPermissions``) must keep its base's guarantee, or
that project's every view goes unreadable. A class that inherits
``BasePermission`` and never overrides it at all is ``AllowAny`` under another
name, which is the single most valuable thing here.
"""

from __future__ import annotations

from djaudit.api import build_api_surface
from djaudit.api.discovery import ApiSurface
from djaudit.api.permissions import (
    DRF_DEFAULT_AUTHENTICATION,
    Defaults,
    Enforcement,
    Source,
    build_guards,
    classify,
    read_defaults,
    resolve,
)
from djaudit.api.views import UNREADABLE
from djaudit.context import ProjectContext
from djaudit.graph.builder import build_model_graph
from djaudit.graph.inheritance import ClassIndex
from djaudit.settings import SettingsView, resolve_all

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

class ModelViewSet(GenericViewSet):
    pass
"""

DRF_PERMISSION_SOURCE = """
class BasePermission:
    def has_permission(self, request, view):
        return True

class AllowAny(BasePermission):
    pass

class IsAuthenticated(BasePermission):
    pass

class IsAdminUser(BasePermission):
    pass

class IsAuthenticatedOrReadOnly(BasePermission):
    pass

class DjangoModelPermissions(BasePermission):
    pass

class DjangoModelPermissionsOrAnonReadOnly(DjangoModelPermissions):
    pass

class DjangoObjectPermissions(DjangoModelPermissions):
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

def authentication_classes(classes):
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

ROUTED = """
from rest_framework import routers
from shop.api import ProjectViewSet

router = routers.DefaultRouter()
router.register('projects', ProjectViewSet)
"""

SETTINGS_MARKERS = "INSTALLED_APPS = []\nSECRET_KEY = 'k'\nDEBUG = False\n"

CONFIGURED = SETTINGS_MARKERS + (
    "REST_FRAMEWORK = {\n"
    "    'DEFAULT_PERMISSION_CLASSES': ['rest_framework.permissions.IsAuthenticated'],\n"
    "    'DEFAULT_AUTHENTICATION_CLASSES': ['rest_framework.authentication.TokenAuthentication'],\n"
    "}\n"
)

NOISY = SETTINGS_MARKERS + (
    "import os\n"
    "REST_FRAMEWORK = {\n"
    "    'PAGE_SIZE': int(os.environ['PAGE_SIZE']),\n"
    "    'DEFAULT_PERMISSION_CLASSES': ['rest_framework.permissions.IsAdminUser'],\n"
    "}\n"
)

UNREADABLE_DEFAULT = SETTINGS_MARKERS + (
    "import os\nREST_FRAMEWORK = {'DEFAULT_PERMISSION_CLASSES': os.environ['PERMS'].split(',')}\n"
)

REQUIRES_LOGIN = SETTINGS_MARKERS + (
    "REST_FRAMEWORK = {'DEFAULT_PERMISSION_CLASSES':"
    " ['rest_framework.permissions.IsAuthenticated']}\n"
)


def project(make_project, api_source: str, **extra: str) -> ProjectContext:
    files = {
        "manage.py": "",
        "rest_framework/__init__.py": "",
        "rest_framework/views.py": DRF_VIEWS,
        "rest_framework/generics.py": DRF_VIEWS,
        "rest_framework/viewsets.py": DRF_VIEWSETS,
        "rest_framework/permissions.py": DRF_PERMISSION_SOURCE,
        "rest_framework/decorators.py": DRF_DECORATORS,
        "rest_framework/routers.py": DRF_ROUTERS,
        "rest_framework/serializers.py": "class ModelSerializer:\n    pass\n",
        "shop/__init__.py": "",
        "shop/models.py": "from django.db import models\n",
        "shop/api.py": api_source,
        "shop/urls.py": ROUTED,
        "shop/settings.py": SETTINGS_MARKERS,
        **extra,
    }
    built: ProjectContext = make_project(files)
    return built


def surface_of(ctx: ProjectContext) -> tuple[ApiSurface, ClassIndex]:
    index = ClassIndex(ctx)
    return build_api_surface(ctx, build_model_graph(ctx), index), index


def settings_view(ctx: ProjectContext) -> SettingsView | None:
    views = resolve_all(ctx)
    return next(iter(views.values())) if views else None


def guard_for(make_project, api_source: str, name: str = "ProjectViewSet", **extra: str):
    """The guard for one view, resolved the way a rule would resolve it."""
    ctx = project(make_project, api_source, **extra)
    surface, index = surface_of(ctx)
    defaults = read_defaults(settings_view(ctx))
    return resolve(surface.views[f"shop.api.{name}"], index, defaults)


class TestDrfDefaults:
    """DRF ships open, so the absence of configuration is itself the finding."""

    def test_nothing_configured_is_allow_any(self, make_project):
        guard = guard_for(
            make_project,
            """
            from rest_framework import viewsets

            class ProjectViewSet(viewsets.ModelViewSet):
                queryset = 1
            """,
        )
        assert guard.enforcement is Enforcement.OPEN
        assert guard.source is Source.DRF_DEFAULT
        assert guard.is_open
        assert guard.allows_anonymous("POST") is True

    def test_absent_settings_module_still_answers(self):
        assert read_defaults(None).permissions == ("rest_framework.permissions.AllowAny",)
        assert read_defaults(None).authentication == DRF_DEFAULT_AUTHENTICATION

    def test_project_default_is_read_from_rest_framework_block(self, make_project):
        ctx = project(
            make_project,
            "x = 1\n",
            **{"shop/settings.py": CONFIGURED},
        )
        defaults = read_defaults(settings_view(ctx))
        assert defaults.source is Source.SETTING
        assert defaults.permissions == ("rest_framework.permissions.IsAuthenticated",)
        assert defaults.authentication == ("rest_framework.authentication.TokenAuthentication",)

    def test_unreadable_sibling_entries_do_not_hide_the_one_we_want(self, make_project):
        """A real ``REST_FRAMEWORK`` block always has something unresolvable in
        it, and reading the dict as a whole would lose everything with it."""
        ctx = project(
            make_project,
            "x = 1\n",
            **{"shop/settings.py": NOISY},
        )
        defaults = read_defaults(settings_view(ctx))
        assert defaults.permissions == ("rest_framework.permissions.IsAdminUser",)
        assert not defaults.permissions_unreadable

    def test_unreadable_default_list_is_not_silently_drf(self, make_project):
        ctx = project(
            make_project,
            "x = 1\n",
            **{"shop/settings.py": UNREADABLE_DEFAULT},
        )
        defaults = read_defaults(settings_view(ctx))
        assert defaults.permissions_unreadable

    def test_view_falls_back_to_the_project_default(self, make_project):
        guard = guard_for(
            make_project,
            """
            from rest_framework import viewsets

            class ProjectViewSet(viewsets.ModelViewSet):
                queryset = 1
            """,
            **{"shop/settings.py": REQUIRES_LOGIN},
        )
        assert guard.enforcement is Enforcement.AUTHENTICATED
        assert guard.source is Source.SETTING
        assert guard.allows_anonymous("GET") is False


class TestDeclaredOnTheView:
    def test_named_class_wins_over_the_default(self, make_project):
        guard = guard_for(
            make_project,
            """
            from rest_framework import viewsets
            from rest_framework.permissions import IsAdminUser

            class ProjectViewSet(viewsets.ModelViewSet):
                permission_classes = [IsAdminUser]
                queryset = 1
            """,
        )
        assert guard.enforcement is Enforcement.STAFF
        assert guard.source is Source.VIEW
        assert guard.declared_by == "shop.api.ProjectViewSet"
        assert guard.classes == ("rest_framework.permissions.IsAdminUser",)

    def test_empty_list_disables_the_check_and_we_are_sure(self, make_project):
        """``permission_classes = []`` is not an absence of information -- DRF
        permits when nothing denies, so it is a deliberate opening."""
        guard = guard_for(
            make_project,
            """
            from rest_framework import viewsets

            class ProjectViewSet(viewsets.ModelViewSet):
                permission_classes = []
                queryset = 1
            """,
            **{"shop/settings.py": REQUIRES_LOGIN},
        )
        assert guard.enforcement is Enforcement.OPEN
        assert guard.is_open
        assert guard.source is Source.VIEW

    def test_empty_tuple_reads_the_same_way(self, make_project):
        guard = guard_for(
            make_project,
            """
            from rest_framework import viewsets

            class ProjectViewSet(viewsets.ModelViewSet):
                permission_classes = ()
                queryset = 1
            """,
        )
        assert guard.is_open

    def test_read_only_permission_is_method_sensitive(self, make_project):
        guard = guard_for(
            make_project,
            """
            from rest_framework import viewsets
            from rest_framework.permissions import IsAuthenticatedOrReadOnly

            class ProjectViewSet(viewsets.ModelViewSet):
                permission_classes = [IsAuthenticatedOrReadOnly]
                queryset = 1
            """,
        )
        assert guard.enforcement is Enforcement.READ_OPEN
        assert guard.allows_anonymous("GET") is True
        assert guard.allows_anonymous("OPTIONS") is True
        assert guard.allows_anonymous("POST") is False
        assert not guard.is_open

    def test_a_list_is_an_and_so_the_strongest_wins(self, make_project):
        guard = guard_for(
            make_project,
            """
            from rest_framework import viewsets
            from rest_framework.permissions import IsAdminUser, IsAuthenticatedOrReadOnly

            class ProjectViewSet(viewsets.ModelViewSet):
                permission_classes = [IsAuthenticatedOrReadOnly, IsAdminUser]
                queryset = 1
            """,
        )
        assert guard.enforcement is Enforcement.STAFF

    def test_composition_is_recorded_as_unreadable_not_as_empty(self, make_project):
        """``A | B`` builds an ``OperandHolder`` whose guarantee is its weakest
        operand. Rendering it to nothing would leave a list that looks empty,
        turning a guarded view into a reported vulnerability."""
        guard = guard_for(
            make_project,
            """
            from rest_framework import viewsets
            from rest_framework.permissions import IsAdminUser, IsAuthenticated

            class ProjectViewSet(viewsets.ModelViewSet):
                permission_classes = [IsAuthenticated | IsAdminUser]
                queryset = 1
            """,
        )
        assert guard.unresolved == (UNREADABLE,)
        assert not guard.certain
        assert not guard.is_open
        assert guard.allows_anonymous("GET") is None

    def test_declaration_is_inherited_from_a_project_base(self, make_project):
        guard = guard_for(
            make_project,
            """
            from rest_framework import viewsets
            from rest_framework.permissions import IsAdminUser

            class BaseViewSet(viewsets.ModelViewSet):
                permission_classes = [IsAdminUser]

            class ProjectViewSet(BaseViewSet):
                queryset = 1
            """,
        )
        assert guard.enforcement is Enforcement.STAFF
        assert guard.source is Source.ANCESTOR
        assert guard.declared_by == "shop.api.BaseViewSet"

    def test_inherited_names_resolve_in_the_ancestors_module(self, make_project):
        """The base writes ``[IsAdminUser]`` in *its* module. Resolving that
        name against the subclass's imports finds nothing, and a view that
        requires staff would come back unreadable."""
        guard = guard_for(
            make_project,
            """
            from shop.base import BaseViewSet

            class ProjectViewSet(BaseViewSet):
                queryset = 1
            """,
            **{
                "shop/base.py": """
                from rest_framework import viewsets
                from rest_framework.permissions import IsAdminUser

                class BaseViewSet(viewsets.ModelViewSet):
                    permission_classes = [IsAdminUser]
                """
            },
        )
        assert guard.classes == ("rest_framework.permissions.IsAdminUser",)
        assert guard.enforcement is Enforcement.STAFF


class TestProjectPermissionClasses:
    def test_base_permission_without_an_override_is_allow_any(self, make_project):
        """``BasePermission.has_permission`` returns ``True``. A class that
        inherits it and never overrides it is ``AllowAny`` with a name that
        says otherwise."""
        guard = guard_for(
            make_project,
            """
            from rest_framework import viewsets
            from rest_framework.permissions import BasePermission

            class LooksSecure(BasePermission):
                def has_object_permission(self, request, view, obj):
                    return obj.owner == request.user

            class ProjectViewSet(viewsets.ModelViewSet):
                permission_classes = [LooksSecure]
                queryset = 1
            """,
        )
        assert guard.enforcement is Enforcement.OPEN
        assert guard.is_open

    def test_hand_written_check_is_unknown_not_guessed(self, make_project):
        guard = guard_for(
            make_project,
            """
            from rest_framework import viewsets
            from rest_framework.permissions import BasePermission

            class IsSuperuser(BasePermission):
                def has_permission(self, request, view):
                    return bool(request.user and request.user.is_superuser)

            class ProjectViewSet(viewsets.ModelViewSet):
                permission_classes = [IsSuperuser]
                queryset = 1
            """,
        )
        assert guard.enforcement is Enforcement.OPEN
        assert guard.unresolved == ("shop.api.IsSuperuser",)
        assert not guard.certain
        assert not guard.is_open

    def test_a_narrowing_override_keeps_the_bases_guarantee(self, make_project):
        """NetBox's ``TokenPermissions`` shape: deny writes from a read-only
        token, otherwise delegate. It can only take permissions away, so the
        base still bounds it -- and 142 of NetBox's 151 views depend on
        reading it that way."""
        guard = guard_for(
            make_project,
            """
            from rest_framework import viewsets
            from rest_framework.permissions import DjangoObjectPermissions

            class TokenPermissions(DjangoObjectPermissions):
                def has_permission(self, request, view):
                    if not self._writable(request):
                        return False
                    return super().has_permission(request, view)

            class ProjectViewSet(viewsets.ModelViewSet):
                permission_classes = [TokenPermissions]
                queryset = 1
            """,
        )
        assert guard.enforcement is Enforcement.AUTHENTICATED
        assert guard.certain

    def test_an_override_that_can_return_true_is_unknown(self, make_project):
        """One ``return True`` on any path is a way past the base, so the
        base's guarantee no longer bounds the class."""
        guard = guard_for(
            make_project,
            """
            from rest_framework import viewsets
            from rest_framework.permissions import DjangoObjectPermissions

            class Loose(DjangoObjectPermissions):
                def has_permission(self, request, view):
                    if request.method == 'GET':
                        return True
                    return super().has_permission(request, view)

            class ProjectViewSet(viewsets.ModelViewSet):
                permission_classes = [Loose]
                queryset = 1
            """,
        )
        assert not guard.certain
        assert guard.unresolved == ("shop.api.Loose",)

    def test_an_override_that_never_delegates_is_unknown(self, make_project):
        ctx = project(
            make_project,
            """
            from rest_framework.permissions import DjangoObjectPermissions

            class NoDelegate(DjangoObjectPermissions):
                def has_permission(self, request, view):
                    return False
            """,
        )
        _, index = surface_of(ctx)
        assert classify("shop.api.NoDelegate", index) is Enforcement.UNKNOWN

    def test_a_subclass_that_only_sets_attributes_inherits(self, make_project):
        ctx = project(
            make_project,
            """
            from rest_framework.permissions import IsAuthenticated

            class Quiet(IsAuthenticated):
                message = 'no'
            """,
        )
        _, index = surface_of(ctx)
        assert classify("shop.api.Quiet", index) is Enforcement.AUTHENTICATED

    def test_a_class_we_cannot_see_is_unknown(self, make_project):
        ctx = project(make_project, "x = 1\n")
        _, index = surface_of(ctx)
        assert classify("oauth2_provider.contrib.rest_framework.TokenHasScope", index) is (
            Enforcement.UNKNOWN
        )

    def test_unknown_and_a_known_class_keeps_the_known_floor(self, make_project):
        """An unreadable class ANDed with ``IsAuthenticated`` still cannot let
        an anonymous caller through, so the floor survives."""
        guard = guard_for(
            make_project,
            """
            from rest_framework import viewsets
            from rest_framework.permissions import BasePermission, IsAuthenticated

            class Custom(BasePermission):
                def has_permission(self, request, view):
                    return request.user.pk % 2 == 0

            class ProjectViewSet(viewsets.ModelViewSet):
                permission_classes = [Custom, IsAuthenticated]
                queryset = 1
            """,
        )
        assert guard.enforcement is Enforcement.AUTHENTICATED
        assert guard.unresolved == ("shop.api.Custom",)


class TestDynamicAndPerAction:
    def test_get_permissions_override_makes_the_attribute_advisory(self, make_project):
        guard = guard_for(
            make_project,
            """
            from rest_framework import viewsets
            from rest_framework.permissions import IsAdminUser

            class ProjectViewSet(viewsets.ModelViewSet):
                permission_classes = [IsAdminUser]
                queryset = 1

                def get_permissions(self):
                    return []
            """,
        )
        assert guard.dynamic
        assert not guard.certain
        assert guard.allows_anonymous("GET") is None

    def test_action_kwargs_override_the_viewset(self, make_project):
        ctx = project(
            make_project,
            """
            from rest_framework import viewsets
            from rest_framework.decorators import action
            from rest_framework.permissions import AllowAny, IsAdminUser

            class ProjectViewSet(viewsets.ModelViewSet):
                permission_classes = [IsAdminUser]
                queryset = 1

                @action(detail=False, permission_classes=[AllowAny])
                def status(self, request):
                    return None
            """,
        )
        surface, index = surface_of(ctx)
        defaults = read_defaults(settings_view(ctx))
        guards = build_guards(surface, index, defaults)
        view = guards.get("shop.api.ProjectViewSet")
        assert view is not None
        assert view.enforcement is Enforcement.STAFF

        override = guards.get("shop.api.ProjectViewSet", "status")
        assert override is not None
        assert override.source is Source.ACTION
        assert override.enforcement is Enforcement.OPEN
        assert override.declared_by == "shop.api.ProjectViewSet.status"

    def test_an_action_without_the_kwarg_uses_the_viewsets_guard(self, make_project):
        ctx = project(
            make_project,
            """
            from rest_framework import viewsets
            from rest_framework.decorators import action
            from rest_framework.permissions import IsAdminUser

            class ProjectViewSet(viewsets.ModelViewSet):
                permission_classes = [IsAdminUser]
                queryset = 1

                @action(detail=False)
                def status(self, request):
                    return None
            """,
        )
        surface, index = surface_of(ctx)
        guards = build_guards(surface, index, read_defaults(settings_view(ctx)))
        assert not guards.actions
        guard = guards.get("shop.api.ProjectViewSet", "status")
        assert guard is not None
        assert guard.enforcement is Enforcement.STAFF


class TestAuthentication:
    def test_absent_declaration_takes_the_project_default(self, make_project):
        guard = guard_for(
            make_project,
            """
            from rest_framework import viewsets

            class ProjectViewSet(viewsets.ModelViewSet):
                queryset = 1
            """,
        )
        assert guard.authentication == DRF_DEFAULT_AUTHENTICATION
        assert not guard.unauthenticated

    def test_an_empty_tuple_means_no_authentication_at_all(self, make_project):
        """pretix writes exactly this on two endpoints. Reading it as "nothing
        declared" would hand them the project's authenticators and hide that
        ``request.user`` is anonymous by construction."""
        guard = guard_for(
            make_project,
            """
            from rest_framework import viewsets

            class ProjectViewSet(viewsets.ModelViewSet):
                authentication_classes = ()
                queryset = 1
            """,
        )
        assert guard.authentication == ()
        assert guard.unauthenticated
        assert guard.authentication_source is Source.VIEW

    def test_declared_authenticators_resolve_to_dotted_paths(self, make_project):
        guard = guard_for(
            make_project,
            """
            from rest_framework import viewsets
            from shop.auth import DeviceAuth

            class ProjectViewSet(viewsets.ModelViewSet):
                authentication_classes = [DeviceAuth]
                queryset = 1
            """,
            **{"shop/auth.py": "class DeviceAuth:\n    pass\n"},
        )
        assert guard.authentication == ("shop.auth.DeviceAuth",)


class TestFunctionViews:
    def test_decorator_permissions_are_resolved(self, make_project):
        ctx = project(
            make_project,
            """
            from rest_framework.decorators import api_view, permission_classes
            from rest_framework.permissions import IsAdminUser

            @api_view(['GET'])
            @permission_classes([IsAdminUser])
            def report(request):
                return None
            """,
        )
        surface, index = surface_of(ctx)
        guard = resolve(surface.views["shop.api.report"], index, read_defaults(settings_view(ctx)))
        assert guard.enforcement is Enforcement.STAFF
        assert guard.classes == ("rest_framework.permissions.IsAdminUser",)

    def test_an_undecorated_function_view_inherits_the_default(self, make_project):
        ctx = project(
            make_project,
            """
            from rest_framework.decorators import api_view

            @api_view(['POST'])
            def report(request):
                return None
            """,
        )
        surface, index = surface_of(ctx)
        guard = resolve(surface.views["shop.api.report"], index, read_defaults(settings_view(ctx)))
        assert guard.is_open
        assert guard.source is Source.DRF_DEFAULT


class TestGuardMap:
    def test_unrouted_views_are_skipped(self, make_project):
        """A base class the project wrote for its own viewsets has no URL, so
        it cannot be an authorization defect."""
        ctx = project(
            make_project,
            """
            from rest_framework import viewsets

            class BaseViewSet(viewsets.ModelViewSet):
                permission_classes = []

            class ProjectViewSet(BaseViewSet):
                queryset = 1
            """,
        )
        surface, index = surface_of(ctx)
        guards = build_guards(surface, index, read_defaults(settings_view(ctx)))
        assert "shop.api.BaseViewSet" not in guards.views
        assert "shop.api.ProjectViewSet" in guards.views
        assert len(guards) == 1

    def test_open_and_uncertain_are_reported_separately(self, make_project):
        ctx = project(
            make_project,
            """
            from rest_framework import viewsets
            from rest_framework.permissions import BasePermission

            class Custom(BasePermission):
                def has_permission(self, request, view):
                    return request.user.pk > 0

            class ProjectViewSet(viewsets.ModelViewSet):
                permission_classes = []
                queryset = 1

            class OtherViewSet(viewsets.ModelViewSet):
                permission_classes = [Custom]
                queryset = 1
            """,
            **{
                "shop/urls.py": ROUTED
                + "from shop.api import OtherViewSet\nrouter.register('other', OtherViewSet)\n"
            },
        )
        surface, index = surface_of(ctx)
        guards = build_guards(surface, index, read_defaults(settings_view(ctx)))
        assert guards.anonymous() == ["shop.api.ProjectViewSet"]
        assert guards.uncertain() == ["shop.api.OtherViewSet"]

    def test_a_missing_view_is_none_rather_than_an_error(self, make_project):
        ctx = project(make_project, "x = 1\n")
        surface, index = surface_of(ctx)
        guards = build_guards(surface, index, Defaults())
        assert guards.get("shop.api.Nope") is None

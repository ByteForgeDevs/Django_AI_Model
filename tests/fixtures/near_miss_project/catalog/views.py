"""Views that scope correctly, written the way real projects write it.

The literal string `objects.all()` appears three times in this file and is
correct every time. Deciding that from the source is the whole job of
DJA-004, and the reason it is the rule most likely to be wrong.
"""

from rest_framework import viewsets
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from .models import AuditEntry, Bookmark, Invoice, Subscription
from .permissions import IsOwner
from .serializers import (
    AccountSerializer,
    BookmarkSerializer,
    InvoiceSerializer,
    ReadOnlyAuditSerializer,
)


class SizedPagination(PageNumberPagination):
    """Not DJA-013. The project sets no `PAGE_SIZE`, and does not need to: a
    pagination class that assigns its own `page_size` has stopped depending on
    the setting entirely. Reading the setting alone reports every project that
    paginates this way.
    """

    page_size = 100
    max_page_size = 500


class ScopedMixin:
    """Rebinds the queryset before the handler runs.

    NetBox's entire object-permission scheme is this shape. The viewsets below
    declare `queryset = Model.objects.all()` and never mention the request,
    and are nonetheless scoped -- by a mixin in another file, in a method DRF
    calls first. A rule reading the class body reports all of them.
    """

    def initial(self, request, *args, **kwargs):
        self.queryset = self.queryset.filter(owner=request.user)
        return super().initial(request, *args, **kwargs)


class BookmarkViewSet(ScopedMixin, viewsets.ModelViewSet):
    """Not DJA-004. `all()` is written here and narrowed in `ScopedMixin`."""

    queryset = Bookmark.objects.all()
    serializer_class = BookmarkSerializer
    permission_classes = [IsAuthenticated, IsOwner]
    pagination_class = SizedPagination

    # Not DJA-014. A named list of two harmless columns, rather than the
    # `'__all__'` that hands the caller every column the model has.
    filterset_fields = ["label", "created"]


class InvoiceViewSet(viewsets.ReadOnlyModelViewSet):
    """Not DJA-004. Every row, on a path only staff reach.

    pretix's `OrganizerViewSet` is written exactly like this. The unscoped
    return is real and is reached only through a condition that reads the
    request, so the expression is unscoped and the path is not.
    """

    serializer_class = InvoiceSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = SizedPagination

    def get_queryset(self):
        if self.request.user.is_staff:
            return Invoice.objects.all()
        return Invoice.objects.filter(account=self.request.user)


class SubscriptionViewSet(viewsets.ModelViewSet):
    """Not DJA-004. Scoped through a manager method rather than a `filter`.

    `for_user` is a name this pattern is given often enough to be worth
    knowing by sight, but the name earns nothing on its own -- what says the
    queryset is narrowed is that the argument came from the request.
    """

    serializer_class = ReadOnlyAuditSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Subscription.objects.for_user(self.request.user)


class AuditEntryViewSet(viewsets.ReadOnlyModelViewSet):
    """Not DJA-004. The subclass refines what its parent already narrowed."""

    serializer_class = ReadOnlyAuditSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = SizedPagination

    def get_queryset(self):
        return AuditEntry.objects.filter(actor=self.request.user)


class RecentAuditViewSet(AuditEntryViewSet):
    """Not DJA-004. `super().get_queryset()` carries the parent's scoping, and
    a rule that reads only this method sees a queryset with no request in it.
    """

    def get_queryset(self):
        return super().get_queryset().order_by("-created")[:100]


class AccountDetailView(APIView):
    """Not DJA-005. The override consults the request, and DRF's own
    `check_object_permissions` is called explicitly -- either alone would be
    enough, and a rule requiring one particular spelling reports the other.
    """

    permission_classes = [IsAuthenticated, IsOwner]
    serializer_class = AccountSerializer

    def get_object(self):
        obj = Invoice.objects.get(pk=self.kwargs["pk"], account=self.request.user)
        self.check_object_permissions(self.request, obj)
        return obj

    def get(self, request, pk):
        return Response({})


class LoginView(APIView):
    """Not DJA-015. A credential endpoint by name, throttled by class.

    `AnonRateThrottle` with a rate for `anon` in `DEFAULT_THROTTLE_RATES` is
    the whole answer, and it takes both halves: naming a throttle class while
    the settings define no rate for its scope throttles nothing at all.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [AnonRateThrottle]

    def post(self, request):
        return Response({})


class StatusView(APIView):
    """Not DJA-007.

    `authentication_classes = []` looks like a mistake and is not one: it is
    paired with `AllowAny`, so nothing downstream expects a user and the two
    settings agree. The contradiction the rule reports is an empty
    authentication list under a permission that still demands a user, where no
    request can satisfy both. Read-only and routed for GET alone, so it is
    also not an anonymous write.
    """

    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        return Response({"status": "ok"})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([AnonRateThrottle])
def password_change(request):
    """Not DJA-006 and not DJA-015. A function view that states its permission
    through the decorator DRF provides for it -- the answer is one line above
    the definition rather than in a class body, and a rule reading only class
    attributes finds nothing and reports the view.
    """
    return Response({})

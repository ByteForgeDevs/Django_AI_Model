"""Views for the DRF recall fixture.

The settings module leaves `DEFAULT_PERMISSION_CLASSES` at `AllowAny`, so a
view that says nothing about permissions is open. Several here say nothing.
"""

from django.contrib.auth import authenticate
from rest_framework import generics, status, viewsets
from rest_framework.decorators import api_view
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from support.models import Comment, Invoice, Profile, Ticket
from support.permissions import IsOwner
from support.serializers import (
    CommentSerializer,
    InvoiceSerializer,
    ProfileSerializer,
    TicketDetailSerializer,
    TicketSerializer,
)


class TicketViewSet(viewsets.ModelViewSet):
    """PLANTED DEFECT (DJA-002, DJA-004): every ticket, to every caller.

    The queryset is the whole table and nothing narrows it to the requesting
    user, so the list hands back every customer's support history and the
    detail route is a direct object reference with no check on it. The view
    also declares no `permission_classes`, so it inherits `AllowAny`.
    """

    queryset = Ticket.objects.all()
    serializer_class = TicketSerializer
    filterset_fields = "__all__"


class TicketDetailView(generics.RetrieveAPIView):
    """PLANTED DEFECT (DJA-005): a get_object that skips the permission check.

    Overriding `get_object` replaces the one place DRF calls
    `check_object_permissions`, so any object permission configured on this
    view is simply never consulted.
    """

    serializer_class = TicketDetailSerializer
    permission_classes = [IsOwner]

    def get_object(self):
        return Ticket.objects.get(pk=self.kwargs["pk"])


class MyTicketViewSet(viewsets.ModelViewSet):
    """Control: the same data, scoped to the caller.

    DJA-004 must stay silent here. This is the shape the rule is asking for,
    and it sits next to the defect so that a rule which reports both is
    obviously wrong rather than subtly noisy.
    """

    serializer_class = TicketSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Ticket.objects.filter(owner=self.request.user)


class CommentViewSet(viewsets.ModelViewSet):
    """Control for scoping through a relation rather than a direct field."""

    serializer_class = CommentSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Comment.objects.filter(ticket__owner=self.request.user)


class InvoiceViewSet(viewsets.ReadOnlyModelViewSet):
    """Control: scoped, permissioned, explicit fields."""

    serializer_class = InvoiceSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Invoice.objects.filter(user=self.request.user)


class ProfileView(generics.RetrieveUpdateAPIView):
    """PLANTED DEFECT (DJA-007): no authentication, but a permission needing one.

    `authentication_classes = []` means `request.user` is always anonymous, and
    `IsAuthenticated` then refuses every caller. The endpoint is not insecure,
    it is unreachable, and it reads as deliberately locked down.
    """

    queryset = Profile.objects.all()
    serializer_class = ProfileSerializer
    authentication_classes = []
    permission_classes = [IsAuthenticated]


class LoginView(APIView):
    """PLANTED DEFECT (DJA-015): unlimited guesses at a password.

    Anonymous, accepts POST, and answers as fast as it is asked because the
    project configures no throttle. That is a credential oracle running at
    network speed.
    """

    authentication_classes = []
    permission_classes = []

    def post(self, request):
        user = authenticate(
            username=request.data.get("username"),
            password=request.data.get("password"),
        )
        if user is None:
            return Response({"detail": "invalid"}, status=status.HTTP_400_BAD_REQUEST)
        return Response({"detail": "ok"})


@api_view(["GET"])
def ticket_export(request):
    """PLANTED DEFECT (DJA-006): a function view with no permission decorator.

    `@api_view` alone brings the project default, which is `AllowAny`, so this
    exports every ticket in the system to anyone who asks.
    """
    data = TicketSerializer(Ticket.objects.all(), many=True).data
    return Response(data)

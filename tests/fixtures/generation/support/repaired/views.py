from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from support.models import Reply, Ticket
from support.serializers import ReplySerializer, TicketSerializer


class TicketViewSet(viewsets.ModelViewSet):
    serializer_class = TicketSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Ticket.objects.filter(reporter=self.request.user).select_related(
            "reporter", "assignee"
        )

    def perform_create(self, serializer):
        serializer.save(reporter=self.request.user)

    @action(detail=False)
    def search(self, request):
        term = request.query_params.get("q", "")
        matches = self.get_queryset().filter(subject__icontains=term)
        return Response([{"id": t.id, "subject": t.subject} for t in matches])


class ReplyViewSet(viewsets.ModelViewSet):
    serializer_class = ReplySerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Reply.objects.filter(ticket__reporter=self.request.user).select_related(
            "author", "ticket"
        )

    def perform_create(self, serializer):
        serializer.save(author=self.request.user)

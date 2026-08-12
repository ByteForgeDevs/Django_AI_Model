from django.db import connection
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from support.models import Reply, Ticket
from support.serializers import ReplySerializer, TicketSerializer


class TicketViewSet(viewsets.ModelViewSet):
    queryset = Ticket.objects.all()
    serializer_class = TicketSerializer

    @action(detail=False)
    def search(self, request):
        term = request.query_params.get("q", "")
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, subject FROM support_ticket WHERE subject LIKE '%%%s%%'" % term
            )
            rows = cursor.fetchall()
        return Response([{"id": r[0], "subject": r[1]} for r in rows])


class ReplyViewSet(viewsets.ModelViewSet):
    queryset = Reply.objects.all()
    serializer_class = ReplySerializer

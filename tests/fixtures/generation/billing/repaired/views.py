from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from billing.models import Invoice, LineItem
from billing.serializers import InvoiceSerializer, LineItemSerializer


class InvoiceViewSet(viewsets.ModelViewSet):
    serializer_class = InvoiceSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        queryset = Invoice.objects.filter(customer=self.request.user).prefetch_related("items")
        paid = self.request.query_params.get("paid")
        if paid is not None:
            queryset = queryset.filter(paid=paid.lower() == "true")
        return queryset

    def perform_create(self, serializer):
        serializer.save(customer=self.request.user)


class LineItemViewSet(viewsets.ModelViewSet):
    serializer_class = LineItemSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return LineItem.objects.filter(invoice__customer=self.request.user).select_related(
            "invoice"
        )

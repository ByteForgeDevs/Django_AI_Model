from rest_framework import viewsets

from billing.models import Invoice, LineItem
from billing.serializers import InvoiceSerializer, LineItemSerializer


class InvoiceViewSet(viewsets.ModelViewSet):
    queryset = Invoice.objects.all()
    serializer_class = InvoiceSerializer

    def get_queryset(self):
        qs = Invoice.objects.all()
        paid = self.request.query_params.get("paid")
        if paid is not None:
            qs = qs.filter(**{"paid": paid})
        return qs


class LineItemViewSet(viewsets.ModelViewSet):
    queryset = LineItem.objects.all()
    serializer_class = LineItemSerializer

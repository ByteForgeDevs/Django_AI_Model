from rest_framework import serializers

from billing.models import Invoice, LineItem


class LineItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = LineItem
        fields = ("id", "invoice", "description", "quantity", "unit_price")


class InvoiceSerializer(serializers.ModelSerializer):
    items = LineItemSerializer(many=True, read_only=True)

    class Meta:
        model = Invoice
        fields = ("id", "number", "customer", "total", "paid", "issued", "items")
        read_only_fields = ("customer", "total", "issued")

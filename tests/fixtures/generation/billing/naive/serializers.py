from rest_framework import serializers

from billing.models import Invoice, LineItem


class LineItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = LineItem
        fields = "__all__"


class InvoiceSerializer(serializers.ModelSerializer):
    items = LineItemSerializer(many=True, read_only=True)

    class Meta:
        model = Invoice
        fields = "__all__"

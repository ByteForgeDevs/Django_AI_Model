from django.db import models


class Invoice(models.Model):
    number = models.CharField(max_length=32, blank=True, default="", db_index=True)
    customer = models.ForeignKey('auth.User', on_delete=models.PROTECT, related_name="invoices")
    total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    paid = models.BooleanField(default=False)
    issued = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-issued",)


class LineItem(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="items")
    description = models.CharField(max_length=200)
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        ordering = ("id",)

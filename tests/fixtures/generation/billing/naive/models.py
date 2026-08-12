from django.db import models


class Invoice(models.Model):
    number = models.CharField(max_length=32, null=True)
    customer = models.ForeignKey('auth.User', on_delete=models.CASCADE)
    total = models.FloatField(default=0)
    paid = models.BooleanField(default=False)
    issued = models.DateTimeField(auto_now_add=True)


class LineItem(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE)
    description = models.CharField(max_length=200)
    quantity = models.IntegerField(default=1)
    unit_price = models.FloatField()

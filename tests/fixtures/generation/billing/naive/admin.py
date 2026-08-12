from django.contrib import admin

from billing.models import Invoice, LineItem

admin.site.register(Invoice)
admin.site.register(LineItem)

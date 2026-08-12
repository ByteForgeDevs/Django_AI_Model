from django.contrib import admin

from support.models import Reply, Ticket

admin.site.register(Ticket)
admin.site.register(Reply)

from django.db import models


class Ticket(models.Model):
    OPEN = "open"
    CLOSED = "closed"
    STATUSES = ((OPEN, "Open"), (CLOSED, "Closed"))

    subject = models.CharField(max_length=200)
    body = models.TextField()
    status = models.CharField(max_length=20, choices=STATUSES, default=OPEN)
    reporter = models.ForeignKey('auth.User', on_delete=models.PROTECT, related_name="tickets")
    assignee = models.ForeignKey(
        'auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name="assigned"
    )
    opened = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-opened",)


class Reply(models.Model):
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="replies")
    author = models.ForeignKey('auth.User', on_delete=models.PROTECT, related_name="replies")
    body = models.TextField()
    internal = models.BooleanField(default=False)

    class Meta:
        ordering = ("id",)

"""Models for the DRF recall fixture.

`Ticket` is the object every IDOR case in this fixture turns on: it has an
owner, so a queryset that does not mention that owner is handing one customer's
support history to another. `Profile` carries the credentials that must never
be serialised. `Invoice` is the retained record.
"""

from django.conf import settings
from django.db import models


class Profile(models.Model):
    """A user's account, including the two fields that must never leave it."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    display_name = models.CharField(max_length=100)
    api_key = models.CharField(max_length=64)
    password_reset_token = models.CharField(max_length=64)

    class Meta:
        ordering = ["pk"]


class Ticket(models.Model):
    """A support ticket. Belongs to exactly one customer."""

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="tickets"
    )
    subject = models.CharField(max_length=200)
    body = models.TextField()
    is_escalated = models.BooleanField(default=False)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created", "pk"]


class Comment(models.Model):
    """A reply on a ticket, reachable only through its ticket."""

    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    body = models.TextField()

    class Meta:
        ordering = ["pk"]


class Invoice(models.Model):
    """PLANTED DEFECT (DJD-001): billing history that leaves with the user.

    `on_delete=CASCADE` on a foreign key to the user means deleting a customer
    deletes what they were charged. `reference` and `tax_note` are the second
    defect (DJD-002): nullable strings with no `blank=True`, so the column has
    both `NULL` and `''` in it and no query can ask for "empty" once.
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    reference = models.CharField(max_length=64, null=True)
    tax_note = models.TextField(null=True)

    class Meta:
        ordering = ["pk"]

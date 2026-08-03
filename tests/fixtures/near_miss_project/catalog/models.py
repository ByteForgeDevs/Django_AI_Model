"""Correct model design written in the shapes the DJD rules look for.

Every class here trips the first half of some rule's test and is then saved by
the half that requires reading Django properly.
"""

from django.conf import settings
from django.db import models


class Ordered(models.Model):
    """Abstract base supplying the ordering its subclasses never mention.

    Not DJD-003. A rule reading only the concrete class's own ``Meta`` sees no
    ordering at all and reports a paginated list as unstable, which would fire
    on most large Django projects -- inheriting ``Meta`` from an abstract base
    is the ordinary way to say this once.
    """

    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        abstract = True
        ordering = ["-created"]


class Bookmark(Ordered):
    """Not DJD-001. A bookmark is a user's own convenience, and cascading is
    the correct answer: nobody wants a deleted user's saved links kept. The
    name carries no obligation to retain anything, which is the only reason
    this differs from an invoice.
    """

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    label = models.CharField(max_length=100)
    url = models.URLField()


class Invoice(Ordered):
    """Not DJD-001. The retained record that is retained.

    ``PROTECT`` is the strongest of the three correct answers and refuses the
    user deletion outright. A rule matching on the model name alone reports
    this line; the rule has to read ``on_delete`` to tell it from the defect.
    """

    account = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    total = models.DecimalField(max_digits=10, decimal_places=2)

    # Not DJD-002. `null=True` with `blank=True` says the project has decided
    # a missing value is permitted and NULL is how it is spelled. The defect
    # is the pair without `blank`, where the column can hold a NULL nothing
    # ever intended to write.
    note = models.CharField(max_length=200, null=True, blank=True)

    # Not DJD-002 either, for a different reason. Django's own documentation
    # gives `null=True` on a unique column as the way to let more than one row
    # have no value -- `""` cannot repeat under a unique index and NULL can.
    external_ref = models.CharField(max_length=64, null=True, unique=True)


class AuditEntry(Ordered):
    """Not DJD-001. ``SET_NULL`` keeps the record and forgets the actor, which
    is what a retention policy usually asks for -- the row survives the user.
    """

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="audit_entries",
    )
    action = models.CharField(max_length=50)


class Recorder(models.Model):
    """Not DJD-001. The substring trap: ``Recorder`` contains ``order`` and
    ``record``, and matching on substrings rather than on CamelCase words
    reports a piece of hardware as a financial record.
    """

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    serial = models.CharField(max_length=40)

    class Meta:
        ordering = ["serial"]


class SubscriptionManager(models.Manager):
    """A manager that orders every queryset and scopes on request."""

    def get_queryset(self) -> models.QuerySet["Subscription"]:
        return super().get_queryset().order_by("topic")

    def for_user(self, user) -> models.QuerySet["Subscription"]:
        return self.get_queryset().filter(user=user)


class Subscription(models.Model):
    """Not DJD-001 and not DJD-003.

    This is NetBox's word: a subscription to change notifications, not to a
    paid plan, and deleting the user should certainly delete it. The default
    manager orders every queryset, so the paginated list is stable without a
    ``Meta.ordering`` the rule can see.
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    topic = models.CharField(max_length=80)

    objects = SubscriptionManager()

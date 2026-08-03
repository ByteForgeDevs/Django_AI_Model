"""Serializers that handle secrets correctly, in the shapes that look wrong.

Every one of these names a field the exposure rules care about. What keeps
them quiet is one keyword each, in a different place every time -- which is
the whole difficulty of reading DRF, and the reason a rule that checks a
single spelling reports projects that did the work.
"""

from django.contrib.auth.models import User
from rest_framework import serializers

from .models import AuditEntry, Bookmark, Invoice


class AccountSerializer(serializers.ModelSerializer):
    """Not DJA-010. Two secrets, two different correct spellings.

    `password` is held back by `extra_kwargs` and `api_key` by `write_only=True`
    on the declared field -- and `Meta.fields` names both in plain sight, so a
    rule reading the field list alone reports a serializer that returns
    neither. `password_changed_at` is the third trap: a name containing
    `password` that holds a timestamp and never a secret, which is why the
    rule matches whole field names rather than substrings.
    """

    api_key = serializers.CharField(write_only=True)

    class Meta:
        model = User
        fields = [
            "id",
            "username",
            "email",
            "password",
            "api_key",
            "password_changed_at",
        ]
        read_only_fields = ["id", "password_changed_at"]
        extra_kwargs = {"password": {"write_only": True}}


class BookmarkSerializer(serializers.ModelSerializer):
    """Not DJA-011. `owner` is a relation to the user model and is writable on
    a naive read -- but `read_only_fields` names it, so the view sets it from
    the request and the client cannot claim another user's row.
    """

    class Meta:
        model = Bookmark
        fields = ["id", "owner", "label", "url", "created"]
        read_only_fields = ["id", "owner", "created"]


class InvoiceSerializer(serializers.ModelSerializer):
    """Not DJA-012. The nested serializer reaches a model with three secrets on
    it, and returns none of them -- so following the nesting one level is not
    enough on its own. The rule has to carry the nested serializer's own
    write-only decisions with it.
    """

    account = AccountSerializer(read_only=True)

    class Meta:
        model = Invoice
        fields = ["id", "account", "total", "note", "external_ref"]
        read_only_fields = ["id", "account"]


class BaseAuditSerializer(serializers.ModelSerializer):
    """Options declared once for a family of serializers."""

    class Meta:
        model = AuditEntry
        fields = ["id", "actor", "action", "created"]
        read_only_fields = ["id", "actor", "action", "created"]


class ReadOnlyAuditSerializer(BaseAuditSerializer):
    """No `Meta` of its own, so Python resolves it through the MRO.

    Documented limitation rather than a near-miss: djaudit records
    `meta_inherited` and declines to guess, so nothing is reported here. The
    entry exists so that the day an ancestor's `Meta` is resolved, this
    serializer is already written and already known to be correct.
    """

    actor = serializers.StringRelatedField(read_only=True)

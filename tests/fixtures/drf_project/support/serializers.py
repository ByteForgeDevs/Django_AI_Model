"""Serializers for the DRF recall fixture.

Each planted defect is a different way to publish more than was intended, and
they are kept in separate classes so a rule that reports the wrong one is
visible in the manifest rather than merely miscounted.
"""

from rest_framework import serializers

from support.models import Comment, Invoice, Profile, Ticket


class ProfileSerializer(serializers.ModelSerializer):
    """PLANTED DEFECT (DJA-008, DJA-010): every field, including the secrets.

    `fields = "__all__"` publishes whatever the model happens to have today,
    which here means `api_key` and `password_reset_token` go out over the wire,
    and means tomorrow's field goes out without anyone deciding it should.
    """

    class Meta:
        model = Profile
        fields = "__all__"


class TicketSerializer(serializers.ModelSerializer):
    """PLANTED DEFECT (DJA-011): `owner` is writable.

    The field that decides who the row belongs to is accepted from the request
    body, so a caller can create or move a ticket into somebody else's account.
    """

    class Meta:
        model = Ticket
        fields = ["id", "owner", "subject", "body", "created"]


class CommentSerializer(serializers.ModelSerializer):
    """PLANTED DEFECT (DJA-009): names what to hide instead of what to show.

    `exclude` inverts the default, so every field added to `Comment` later is
    published automatically and the omission is invisible in this file.
    """

    class Meta:
        model = Comment
        exclude = ["ticket"]


class AccountSerializer(serializers.ModelSerializer):
    """PLANTED DEFECT (DJA-010): names a credential in an explicit field list.

    Unlike `ProfileSerializer` this one was written field by field, so somebody
    read the list and `api_key` survived the reading. That is a different
    mistake from `__all__` and needs to be reported separately -- one is an
    omission, the other is a decision.
    """

    class Meta:
        model = Profile
        fields = ["id", "display_name", "api_key"]


class TicketDetailSerializer(serializers.ModelSerializer):
    """PLANTED DEFECT (DJA-012): reaches a secret through a nested serializer.

    Nothing here names `api_key`. `AccountSerializer` does, and nesting it
    carries the whole thing along -- which is why the nested case needs its own
    rule rather than being a variant of DJA-010.
    """

    owner_profile = AccountSerializer(read_only=True)

    class Meta:
        model = Ticket
        fields = ["id", "subject", "body", "owner_profile"]


class InvoiceSerializer(serializers.ModelSerializer):
    """Control: an explicit field list with nothing sensitive in it.

    Every exposure rule must stay silent here, which is what makes this file a
    precision test as well as a recall one.
    """

    class Meta:
        model = Invoice
        fields = ["id", "amount", "reference"]
        read_only_fields = ["id", "amount"]

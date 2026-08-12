from rest_framework import serializers

from support.models import Reply, Ticket


class ReplySerializer(serializers.ModelSerializer):
    class Meta:
        model = Reply
        fields = ("id", "ticket", "author", "body", "internal")
        read_only_fields = ("author",)


class TicketSerializer(serializers.ModelSerializer):
    replies = ReplySerializer(many=True, read_only=True)

    class Meta:
        model = Ticket
        fields = ("id", "subject", "body", "status", "reporter", "assignee", "opened", "replies")
        read_only_fields = ("reporter", "opened")

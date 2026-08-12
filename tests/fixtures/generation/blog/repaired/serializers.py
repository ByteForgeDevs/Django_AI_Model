from rest_framework import serializers

from blog.models import Comment, Post


class CommentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Comment
        fields = ("id", "post", "author", "body", "created")
        read_only_fields = ("author", "created")


class PostSerializer(serializers.ModelSerializer):
    comments = CommentSerializer(many=True, read_only=True)

    class Meta:
        model = Post
        fields = ("id", "title", "slug", "body", "author", "published", "created", "comments")
        read_only_fields = ("author", "created")

from django.db.models import Q
from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from blog.models import Comment, Post
from blog.serializers import CommentSerializer, PostSerializer


class PostViewSet(viewsets.ModelViewSet):
    serializer_class = PostSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return (
            Post.objects.filter(Q(published=True) | Q(author=self.request.user))
            .select_related("author")
            .prefetch_related("comments__author")
        )

    def perform_create(self, serializer):
        serializer.save(author=self.request.user)


class CommentViewSet(viewsets.ModelViewSet):
    serializer_class = CommentSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Comment.objects.filter(
            Q(post__published=True) | Q(post__author=self.request.user)
        ).select_related("author", "post")

    def perform_create(self, serializer):
        serializer.save(author=self.request.user)

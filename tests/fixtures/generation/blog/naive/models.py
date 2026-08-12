from django.db import models


class Post(models.Model):
    title = models.CharField(max_length=200)
    slug = models.CharField(max_length=200, null=True)
    body = models.TextField()
    author = models.ForeignKey('auth.User', on_delete=models.CASCADE)
    published = models.BooleanField(default=False)
    created = models.DateTimeField(auto_now_add=True)


class Comment(models.Model):
    post = models.ForeignKey(Post, on_delete=models.CASCADE)
    author = models.ForeignKey('auth.User', on_delete=models.CASCADE)
    body = models.TextField()
    created = models.DateTimeField(auto_now_add=True)

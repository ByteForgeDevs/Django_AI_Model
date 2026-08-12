from django.db import models


class Ticket(models.Model):
    subject = models.CharField(max_length=200)
    body = models.TextField()
    status = models.CharField(max_length=20, default='open')
    reporter = models.ForeignKey('auth.User', on_delete=models.CASCADE)
    assignee = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True)
    opened = models.DateTimeField(auto_now_add=True)


class Reply(models.Model):
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE)
    author = models.ForeignKey('auth.User', on_delete=models.CASCADE)
    body = models.TextField()
    internal = models.BooleanField(default=False)

"""Fixture reporter filter. Never imported -- djaudit only parses it.

Control for DJS-027: this *extends* Django's filter and adds to what it
redacts, which is the shape of somebody who thought about the problem. The
defect is a class that replaces it.
"""

from django.views.debug import SafeExceptionReporterFilter


class StricterFilter(SafeExceptionReporterFilter):
    hidden_settings = SafeExceptionReporterFilter.hidden_settings

    def get_post_parameters(self, request):
        return {}

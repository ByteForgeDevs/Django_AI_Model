"""Fixture module. Never imported -- djaudit only parses it.

The control for DJS-027: extending Django's filter rather than replacing it is
the right way to redact more, and must stay silent.
"""

from django.views.debug import SafeExceptionReporterFilter


class ExtraFilter(SafeExceptionReporterFilter):
    def cleanse_setting(self, key, value):
        return super().cleanse_setting(key, value)

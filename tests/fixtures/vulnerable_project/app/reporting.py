"""Fixture module. Never imported -- djaudit only parses it.

PLANTED DEFECT: this replaces SafeExceptionReporterFilter instead of extending
it, so the pattern that redacts AUTH, TOKEN, KEY, SECRET, PASS and HTTP_COOKIE
is gone and whatever this class does is all the protection there is.
"""


class TerseFilter:
    def is_active(self, request):
        return True

"""Measure which related-manager methods `prefetch_related` actually serves.

DJP-002 says "add `prefetch_related`". That advice is only correct for reads
that come back out of the prefetch cache, and which reads those are is not
something to settle by reading the ORM source and hoping. This script settles
it by counting queries against a real SQLite database, once without the
prefetch and once with it.

The answer that matters is the second column going *up*: for every method that
clones the queryset, prefetching adds a query and removes none, so a rule that
recommended it would be making the code slower. Only `all`, `count` and
`exists` come back out of the cache, and that measurement is what
`CACHE_READS` in `djaudit.rules.performance` encodes.

Run: `uv run python scripts/prefetch_cache_probe.py`
"""

from __future__ import annotations

import sys
import types
from collections.abc import Callable
from typing import Any

import django
from django.conf import settings

settings.configure(
    DEBUG=True,
    DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
    INSTALLED_APPS=[],
    USE_TZ=True,
)
django.setup()

# Defined after `setup()` because a model class registers itself on definition.
_models = types.ModuleType("probeapp.models")
exec(
    compile(
        """
from django.db import models


class VM(models.Model):
    name = models.CharField(max_length=10)

    class Meta:
        app_label = "probeapp"


class Iface(models.Model):
    vm = models.ForeignKey(VM, on_delete=models.CASCADE, related_name="interfaces")

    class Meta:
        app_label = "probeapp"
""",
        "<probeapp.models>",
        "exec",
    ),
    _models.__dict__,
)
sys.modules["probeapp.models"] = _models

from django.db import connection  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402

VM = _models.VM
Iface = _models.Iface

CASES: dict[str, Callable[[Any], object]] = {
    ".all()": lambda vm: list(vm.interfaces.all()),
    "len(.all())": lambda vm: len(vm.interfaces.all()),
    ".count()": lambda vm: vm.interfaces.count(),
    ".exists()": lambda vm: vm.interfaces.exists(),
    ".values_list()": lambda vm: list(vm.interfaces.values_list("id", flat=True)),
    ".all().values_list()": lambda vm: list(vm.interfaces.all().values_list("id", flat=True)),
    ".filter(...)": lambda vm: list(vm.interfaces.filter(id__gt=0)),
    ".first()": lambda vm: vm.interfaces.first(),
    ".order_by()[:1]": lambda vm: list(vm.interfaces.order_by("pk")[:1]),
    ".iterator()": lambda vm: list(vm.interfaces.iterator()),
}

EXPECTED_TO_HELP = {".all()", "len(.all())", ".count()", ".exists()"}


def queries(fn: Callable[[Any], object], *, prefetch: bool) -> int:
    qs = VM.objects.prefetch_related("interfaces") if prefetch else VM.objects.all()
    with CaptureQueriesContext(connection) as captured:
        for vm in qs:
            fn(vm)
    return len(captured)


def main() -> int:
    with connection.schema_editor() as editor:
        editor.create_model(VM)
        editor.create_model(Iface)
    for i in range(3):
        vm = VM.objects.create(name=f"v{i}")
        for _ in range(2):
            Iface.objects.create(vm=vm)

    print(f"{'expression':24} {'plain':>6} {'prefetched':>11}  verdict")
    wrong = []
    for label, fn in CASES.items():
        plain = queries(fn, prefetch=False)
        pre = queries(fn, prefetch=True)
        helps = pre < plain
        print(f"{label:24} {plain:>6} {pre:>11}  {'HELPS' if helps else 'NO HELP'}")
        if helps != (label in EXPECTED_TO_HELP):
            wrong.append(label)

    if wrong:
        print(f"\nFAIL: Django no longer behaves as CACHE_READS assumes: {wrong}")
        return 1
    print("\nOK: matches CACHE_READS in djaudit.rules.performance")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

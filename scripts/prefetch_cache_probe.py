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
import tracemalloc
import types
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
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
    site = models.ForeignKey("Site", on_delete=models.CASCADE, null=True)

    class Meta:
        app_label = "probeapp"


class Site(models.Model):
    name = models.CharField(max_length=10)

    class Meta:
        app_label = "probeapp"


class Iface(models.Model):
    vm = models.ForeignKey(VM, on_delete=models.CASCADE, related_name="interfaces")
    site = models.ForeignKey("Site", on_delete=models.CASCADE, null=True)

    class Meta:
        app_label = "probeapp"


class Note(models.Model):
    text = models.CharField(max_length=20)
    touched = models.DateTimeField(auto_now=True)

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
from django.db.models import Prefetch  # noqa: E402
from django.db.models.signals import post_save  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402

WRITE_ROWS = 20

BULK_CEILING = 5
"""A SELECT, a BEGIN, the statement and a COMMIT, with one to spare."""

LONG_AGO = datetime(2000, 1, 1, tzinfo=UTC)

STREAM_ROWS = 20000
"""Rows for the materialise-versus-stream comparison.

Must exceed `iterator()`'s default `chunk_size`, which is **2000**. The first
version of this gate used exactly 2000 rows and failed at a ratio of 2.5x --
not because the claim was wrong but because at the chunk size `iterator()`
holds every row too, so the two arms were measuring the same thing. At 20000
the streaming arm peaks at roughly one chunk's worth, which is the shape the
rule actually depends on.
"""

STREAM_RATIO = 5
"""How much heavier materialising must be before the gate believes it.

Measured at 9x. The margin is left wide because the absolute numbers depend on
row width and on the interpreter, while the shape does not.
"""
"""A sentinel `auto_now` value. Compared with `__lt`, never `__year`: on this
SQLite build `__year` matches nothing at all, so a gate written with it would
have passed by absence for both branches."""

VM = _models.VM
Iface = _models.Iface
Site = _models.Site
Note = _models.Note

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

FORWARD: dict[str, Callable[[], object]] = {
    "no fetch": lambda: Iface.objects.all(),  # noqa: PLW0108  (parallel to the rest)
    "select_related('vm')": lambda: Iface.objects.select_related("vm"),
    "prefetch_related('vm')": lambda: Iface.objects.prefetch_related("vm"),
    "prefetch_related('vm__site')": lambda: Iface.objects.prefetch_related("vm__site"),
}
"""Reading `obj.vm` -- a *forward* foreign key -- under each fetch form.

`ChainSpec.covers` treats `select_related` and `prefetch_related` as
interchangeable for a forward relation and honours a longer path as covering
its prefix. Both halves of that are claims about Django, so both are measured:
everything below `no fetch` must cost strictly fewer queries, including the
`vm__site` case that never names `vm` on its own.
"""


def queries(fn: Callable[[Any], object], *, prefetch: bool) -> int:
    qs = VM.objects.prefetch_related("interfaces") if prefetch else VM.objects.all()
    with CaptureQueriesContext(connection) as captured:
        for vm in qs:
            fn(vm)
    return len(captured)


TO_ATTR: dict[str, tuple[Callable[[], object], Callable[[Any], object]]] = {
    "plain prefetch, read .interfaces": (
        lambda: VM.objects.prefetch_related("interfaces"),
        lambda vm: list(vm.interfaces.all()),
    ),
    "to_attr prefetch, read .recent": (
        lambda: VM.objects.prefetch_related(Prefetch("interfaces", to_attr="recent")),
        lambda vm: list(vm.recent),
    ),
    "to_attr prefetch, read .interfaces": (
        lambda: VM.objects.prefetch_related(Prefetch("interfaces", to_attr="recent")),
        lambda vm: list(vm.interfaces.all()),
    ),
}
"""What `to_attr` does and does not populate.

`ChainSpec` keys prefetches by lookup, so `Prefetch("interfaces", to_attr=...)`
and a plain `prefetch_related("interfaces")` look identical to it. Django does
not treat them the same: `to_attr` puts the rows on a new attribute and leaves
the ordinary related manager unprefetched. If that is right, the third case
costs one query per row and a rule that reads the lookup alone would call a
real N+1 covered. Measured rather than assumed.
"""

EXPECTED_SERVED = {"plain prefetch, read .interfaces", "to_attr prefetch, read .recent"}


def to_attr_queries(build: Callable[[], object], read: Callable[[Any], object]) -> int:
    with CaptureQueriesContext(connection) as captured:
        for row in build():  # type: ignore[attr-defined]
            read(row)
    return len(captured)


NESTED: dict[str, Callable[[], object]] = {
    "prefetch_related('interfaces')": lambda: VM.objects.prefetch_related("interfaces"),
    "Prefetch(queryset=select_related('site'))": lambda: VM.objects.prefetch_related(
        Prefetch("interfaces", queryset=Iface.objects.select_related("site"))
    ),
    "Prefetch(queryset=prefetch_related('site'))": lambda: VM.objects.prefetch_related(
        Prefetch("interfaces", queryset=Iface.objects.prefetch_related("site"))
    ),
    "prefetch_related('interfaces__site')": lambda: VM.objects.prefetch_related("interfaces__site"),
}
"""Whether a `Prefetch`'s inner queryset covers the path *below* the lookup.

`ChainSpec` reads the lookup and drops the `queryset=` argument on the floor, so
`Prefetch("interfaces", queryset=Iface.objects.select_related("site"))` records
`interfaces` and nothing more -- and a loop reading `iface.site` is reported as
an N+1 the author has in fact already fixed. Whether that is really a false
positive is a claim about Django, so it is measured: every form below must beat
the plain-prefetch baseline, which pays one query per interface.
"""


def nested_queries(build: Callable[[], object]) -> int:
    """Queries spent reading `iface.site` for every interface of every VM."""
    with CaptureQueriesContext(connection) as captured:
        for vm in build():  # type: ignore[attr-defined]
            for iface in vm.interfaces.all():
                _ = iface.site
    return len(captured)


def forward_queries(build: Callable[[], object]) -> int:
    """Queries spent reading `obj.vm` across every row of `build()`."""
    with CaptureQueriesContext(connection) as captured:
        for row in build():  # type: ignore[attr-defined]
            _ = row.vm
    return len(captured)


EMPTINESS: dict[str, Callable[[], object]] = {
    ".count() > 0": lambda: Iface.objects.count() > 0,
    ".exists()": Iface.objects.exists,
}
"""What an emptiness test actually costs, for `DJP-006`.

Both forms spend exactly one query, so a rule that counted queries would find
no difference and conclude there is nothing to report. The difference is in the
SQL: `.exists()` adds `LIMIT 1` and stops at the first row, while `.count()`
aggregates over every matching row. So this measures the *statement*, not the
count, and the gate is the presence of `LIMIT` -- the only observable reason
`DJP-006` exists.
"""


def emptiness_sql(fn: Callable[[], object]) -> str:
    """The single statement an emptiness test emits."""
    with CaptureQueriesContext(connection) as captured:
        fn()
    if len(captured) != 1:
        return f"<{len(captured)} queries>"
    return " ".join(captured[0]["sql"].split())


def prefetched_emptiness(method: str) -> int:
    """Queries spent asking whether a *prefetched* related set is empty.

    `DJP-005` excludes related accessors because `len()` reads the prefetch
    cache. The same question has to be asked of `DJP-006` before it can claim
    `.count()` is worth replacing on one: if both forms are already free here,
    the advice is neutral rather than useful.
    """
    qs = VM.objects.prefetch_related("interfaces")
    with CaptureQueriesContext(connection) as captured:
        for vm in qs:
            _ = getattr(vm.interfaces, method)() > 0
    return len(captured)


def seed_notes() -> None:
    """Reset to a known row count, so no measurement depends on the last one."""
    Note.objects.all().delete()
    Note.objects.bulk_create([Note(text="before") for _ in range(WRITE_ROWS)])


def per_row_writes(bulk: bool) -> int:
    """Queries spent updating every row of a table, one way or the other."""
    seed_notes()
    with CaptureQueriesContext(connection) as captured:
        if bulk:
            rows = list(Note.objects.all())
            for row in rows:
                row.text = "after"
            Note.objects.bulk_update(rows, ["text"])
        else:
            for row in Note.objects.all():
                row.text = "after"
                row.save()
    return len(captured)


def per_row_inserts(bulk: bool) -> int:
    """Queries spent creating rows one at a time against `bulk_create`."""
    Note.objects.all().delete()  # the measurement is the inserts themselves
    with CaptureQueriesContext(connection) as captured:
        if bulk:
            Note.objects.bulk_create([Note(text=f"n{i}") for i in range(WRITE_ROWS)])
        else:
            for i in range(WRITE_ROWS):
                Note.objects.create(text=f"n{i}")
    return len(captured)


def signals_fired(bulk: bool) -> int:
    """`post_save` deliveries for the same logical change.

    This is why DJP-007 declines a model with a save signal: `bulk_update`
    is not a drop-in replacement when something is listening.
    """
    seen = []
    receiver = lambda **kw: seen.append(kw["instance"])  # noqa: E731
    post_save.connect(receiver, sender=Note)
    try:
        per_row_writes(bulk)
    finally:
        post_save.disconnect(receiver, sender=Note)
    return len(seen)


def auto_now_moved(bulk: bool) -> bool:
    """Whether the `auto_now` column advanced, which only `save()` guarantees."""
    seed_notes()
    Note.objects.update(touched=LONG_AGO)
    rows = list(Note.objects.all())
    for row in rows:
        row.text = "after"
    if bulk:
        Note.objects.bulk_update(rows, ["text"])
    else:
        for row in rows:
            row.save()
    stale = Note.objects.filter(touched__lt=LONG_AGO + timedelta(days=1)).count()
    return bool(stale == 0)


def writes_report() -> list[str]:
    """The `DJP-007` half: what a per-row write costs, and what bulk_* skips."""
    failures: list[str] = []
    print(f"\n{'per-row write over ' + str(WRITE_ROWS) + ' rows':38} {'queries':>8}")
    spent = {}
    for label, bulk in (("save() in a loop", False), ("bulk_update()", True)):
        spent[label] = per_row_writes(bulk)
        print(f"{label:38} {spent[label]:>8}")
    # The claim is the shape, not the margin: one form grows with the row
    # count and the other does not. A two-row table cannot show that, which is
    # why WRITE_ROWS is large enough for the two to be unmistakable.
    if not (spent["save() in a loop"] >= WRITE_ROWS and spent["bulk_update()"] <= BULK_CEILING):
        failures.append("bulk_update does not flatten the query count")

    print(f"\n{'per-row insert':38} {'queries':>8}")
    for label, bulk in (("create() in a loop", False), ("bulk_create()", True)):
        spent[label] = per_row_inserts(bulk)
        print(f"{label:38} {spent[label]:>8}")
    if not (spent["create() in a loop"] >= WRITE_ROWS and spent["bulk_create()"] <= BULK_CEILING):
        failures.append("bulk_create does not flatten the query count")

    print(f"\n{'what bulk_update skips':38} {'save()':>8} {'bulk':>8}")
    fired = (signals_fired(False), signals_fired(True))
    print(f"{'post_save deliveries':38} {fired[0]:>8} {fired[1]:>8}")
    if not (fired[0] == WRITE_ROWS and fired[1] == 0):
        failures.append("post_save signal behaviour")

    moved = (auto_now_moved(False), auto_now_moved(True))
    print(f"{'auto_now column advanced':38} {moved[0]!s:>8} {moved[1]!s:>8}")
    if not (moved[0] and not moved[1]):
        failures.append("auto_now behaviour")
    return failures


def peak_bytes(materialise: bool) -> tuple[int, int]:
    """Peak allocation, and rows alive at once, reading every Note.

    `list(qs)` builds the whole result cache before the first row is used;
    `.iterator()` streams server-side and never populates it. The row count is
    taken from `_result_cache` rather than from memory, because it is the fact
    the rule actually depends on -- memory is the consequence.
    """
    Note.objects.all().delete()
    Note.objects.bulk_create([Note(text=f"n{i}" * 20) for i in range(STREAM_ROWS)])
    queryset = Note.objects.all()
    tracemalloc.start()
    tracemalloc.reset_peak()
    if materialise:
        rows = list(queryset)
        held = len(queryset._result_cache or [])
        total = sum(len(row.text) for row in rows)
    else:
        total = sum(len(row.text) for row in queryset.iterator())
        held = len(queryset._result_cache or [])
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert total  # the rows really were read, in both arms
    return peak, held


def streaming_report() -> list[str]:
    """The `DJP-008` half: what materialising a whole table costs."""
    failures: list[str] = []
    print(f"\n{'reading ' + str(STREAM_ROWS) + ' rows':38} {'peak B':>10} {'cached':>8}")
    peaks = {}
    for label, materialise in (("list(qs)", True), ("qs.iterator()", False)):
        peaks[label] = peak_bytes(materialise)
        print(f"{label:38} {peaks[label][0]:>10} {peaks[label][1]:>8}")
    # Two separate claims. The cache one is exact and is what the rule reads
    # from the source; the memory one is the reason anybody cares.
    if not (peaks["list(qs)"][1] == STREAM_ROWS and peaks["qs.iterator()"][1] == 0):
        failures.append("iterator() populates the result cache")
    if not peaks["list(qs)"][0] > peaks["qs.iterator()"][0] * STREAM_RATIO:
        failures.append("materialising is not measurably heavier than streaming")
    return failures


def emptiness_report() -> list[str]:
    """The `DJP-006` half: what an emptiness test costs, and what it emits."""
    failures: list[str] = []
    print(f"\n{'emptiness test':16} SQL")
    limited = {}
    for label, ask in EMPTINESS.items():
        sql = emptiness_sql(ask)
        limited[label] = "LIMIT" in sql.upper()
        print(f"{label:16} {sql}")
    if limited != {".count() > 0": False, ".exists()": True}:
        failures.append("emptiness SQL")

    print(f"\n{'prefetched .interfaces emptiness':38} {'queries':>8}  verdict")
    for method in ("count", "exists"):
        spent = prefetched_emptiness(method)
        served = spent <= 2
        label = f".{method}() on a prefetched set"
        print(f"{label:38} {spent:>8}  {'SERVED' if served else 'EXTRA'}")
        if not served:
            failures.append(f"prefetched {method}")
    return failures


def main() -> int:
    with connection.schema_editor() as editor:
        editor.create_model(Site)
        editor.create_model(VM)
        editor.create_model(Iface)
        editor.create_model(Note)
    for i in range(3):
        vm = VM.objects.create(name=f"v{i}", site=Site.objects.create(name=f"s{i}"))
        for _ in range(2):
            Iface.objects.create(vm=vm, site=vm.site)

    print(f"{'expression':24} {'plain':>6} {'prefetched':>11}  verdict")
    wrong = []
    for label, fn in CASES.items():
        plain = queries(fn, prefetch=False)
        pre = queries(fn, prefetch=True)
        helps = pre < plain
        print(f"{label:24} {plain:>6} {pre:>11}  {'HELPS' if helps else 'NO HELP'}")
        if helps != (label in EXPECTED_TO_HELP):
            wrong.append(label)

    print(f"\n{'forward FK: reading obj.vm':32} {'queries':>8}  verdict")
    baseline = forward_queries(FORWARD["no fetch"])
    for label, build in FORWARD.items():
        spent = forward_queries(build)
        covered = spent < baseline
        verdict = "baseline" if label == "no fetch" else ("COVERS" if covered else "NO HELP")
        print(f"{label:32} {spent:>8}  {verdict}")
        if label != "no fetch" and not covered:
            wrong.append(label)

    print(f"\n{'to_attr: which read is served':38} {'queries':>8}  verdict")
    rows = VM.objects.count()
    for label, (build, read) in TO_ATTR.items():
        spent = to_attr_queries(build, read)
        served = spent <= 2
        print(f"{label:38} {spent:>8}  {'SERVED' if served else f'{rows} EXTRA'}")
        if served != (label in EXPECTED_SERVED):
            wrong.append(label)

    print(f"\n{'nested queryset: reading iface.site':44} {'queries':>8}  verdict")
    base = nested_queries(NESTED["prefetch_related('interfaces')"])
    for label, build in NESTED.items():
        spent = nested_queries(build)
        covered = spent < base
        verdict = (
            "baseline"
            if spent == base and label.startswith("prefetch_related('i") and "__" not in label
            else ("COVERS" if covered else "NO HELP")
        )
        print(f"{label:44} {spent:>8}  {verdict}")
        if label != "prefetch_related('interfaces')" and not covered:
            wrong.append(label)

    wrong.extend(emptiness_report())
    wrong.extend(writes_report())
    wrong.extend(streaming_report())

    if wrong:
        print(f"\nFAIL: Django no longer behaves as CACHE_READS assumes: {wrong}")
        return 1
    print("\nOK: matches CACHE_READS in djaudit.rules.performance")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

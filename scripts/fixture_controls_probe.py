"""Prove every control in the ORM fixture is load-bearing, and still aimed.

A control passes by producing no finding, and so does a control the rule can
never reach. This removes the fix from each one and requires the matching rule
to then report it. Running it found one control that was worthless: DJP-010
only speaks about models carrying a self-stamping timestamp, so a control
viewset over an untimestamped model passed by being invisible.

It then does a second job. Most controls are guarded by file -- the manifest
forbids any finding in `inventory/controls.py` at all -- but the two that live
beside their defect have to be guarded by line, and a line number rots. It
rotted within an hour of being written here, by four lines, because a docstring
above it grew. So the un-fix also reports where the rule *would* speak, and
every line-numbered entry in the manifest is checked against that.
"""

from __future__ import annotations

import json
from pathlib import Path

from djaudit import engine
from djaudit.models import Confidence, Severity

ROOT = Path(__file__).resolve().parent.parent
ORM = ROOT / "tests/fixtures/orm_project"
CONTROLS = ORM / "inventory/controls.py"
VIEWS = ORM / "inventory/views.py"

INJECTION = ROOT / "tests/fixtures/injection_project"
ICONTROLS = INJECTION / "shop/controls.py"

PORTABILITY = ROOT / "tests/fixtures/portability_project"
PQUERIES = PORTABILITY / "warehouse/queries.py"

Unfix = tuple[str, str, Path, str, str]

ORM_UNFIXES: tuple[Unfix, ...] = (
    (
        "DJP-001",
        "drop select_related from the fetched loop",
        CONTROLS,
        'for device in Device.objects.select_related("site"):',
        "for device in Device.objects.all():",
    ),
    (
        "DJP-002",
        "drop prefetch_related from the reverse-relation loop",
        CONTROLS,
        'for device in Device.objects.prefetch_related("interfaces"):',
        "for device in Device.objects.all():",
    ),
    (
        "DJP-002",
        "drop the Prefetch object from the many-to-many loop",
        CONTROLS,
        'for device in Device.objects.prefetch_related(Prefetch("tags")):',
        "for device in Device.objects.all():",
    ),
    (
        "DJP-001",
        "reduce the nested Prefetch to a bare relation name",
        CONTROLS,
        '        Prefetch("device", queryset=Device.objects.select_related("site"))',
        '        Prefetch("device")',
    ),
    (
        "DJP-002",
        "read the relation the to_attr prefetch renamed away",
        CONTROLS,
        '        rows.append({"device": device.name, "live": len(device.live)})',
        '        rows.append({"device": device.name, "live": len(device.interfaces.all())})',
    ),
    (
        "DJP-005",
        "stop reading the rows len() counted",
        CONTROLS,
        '    return JsonResponse({"total": total, "names": [device.name for device in devices]})',
        '    return JsonResponse({"total": total})',
    ),
    (
        "DJP-006",
        "ask for a count where existence was asked for",
        CONTROLS,
        "    if Device.objects.filter(site_id=pk).exists():",
        "    if Device.objects.filter(site_id=pk).count() > 0:",
    ),
    (
        "DJP-007",
        "write the bulk update back one row at a time",
        CONTROLS,
        '    sites = list(Site.objects.filter(region="emea"))\n'
        "    for site in sites:\n"
        '        site.region = "eu"\n'
        '    Site.objects.bulk_update(sites, ["region"])',
        '    for site in Site.objects.filter(region="emea"):\n'
        '        site.region = "eu"\n'
        "        site.save()",
    ),
    (
        "DJP-009",
        "leave the read column out of only()",
        CONTROLS,
        'for device in Device.objects.only("name", "serial"):',
        'for device in Device.objects.only("name"):',
    ),
    (
        "DJP-003",
        "stop fetching the relation the serializer walks",
        VIEWS,
        '    queryset = Device.objects.select_related("site")\n'
        "    serializer_class = FetchedDeviceSerializer\n\n"
        "    def get_queryset(self):\n"
        '        return Device.objects.filter(owner=self.request.user).select_related("site")',
        "    queryset = Device.objects.all()\n"
        "    serializer_class = FetchedDeviceSerializer\n\n"
        "    def get_queryset(self):\n"
        "        return Device.objects.filter(owner=self.request.user)",
    ),
    (
        "DJP-010",
        "add an unindexed column to the allowed sort list",
        VIEWS,
        '    ordering_fields = ["created"]',
        '    ordering_fields = ["created", "detail"]',
    ),
)

INJECTION_UNFIXES: tuple[Unfix, ...] = (
    (
        "DJI-001",
        "interpolate the term instead of passing it as a parameter",
        ICONTROLS,
        '        cursor.execute("SELECT id, name FROM shop_product WHERE name LIKE %s", '
        '[f"%{term}%"])',
        "        cursor.execute(f\"SELECT id, name FROM shop_product WHERE name LIKE '%{term}%'\")",
    ),
    (
        "DJI-002",
        "interpolate the sku into the raw query",
        ICONTROLS,
        "    found = Product.objects.raw(\n"
        '        "SELECT * FROM shop_product WHERE sku = %s", [request.GET["sku"]]\n'
        "    )",
        "    found = Product.objects.raw(\n"
        '        "SELECT * FROM shop_product WHERE sku = \'%s\'" % request.GET["sku"]\n'
        "    )",
    ),
    (
        "DJI-003",
        "drop extra()'s params and build the clause by hand",
        ICONTROLS,
        "    found = Product.objects.extra(\n"
        '        where=["price > %s"], params=[request.GET["min_price"]]\n'
        "    )",
        '    found = Product.objects.extra(where=["price > " + request.GET["min_price"]])',
    ),
    (
        "DJI-004",
        "interpolate the weight into the RawSQL text",
        ICONTROLS,
        "    found = Product.objects.annotate(\n"
        '        rank=RawSQL("price * %s", (request.GET["weight"],))\n'
        "    )",
        "    found = Product.objects.annotate(\n"
        "        rank=RawSQL(f\"price * {request.GET['weight']}\", ())\n"
        "    )",
    ),
    (
        "DJI-005",
        "expand the query string instead of choosing the lookups",
        ICONTROLS,
        "    found = Product.objects.filter(**criteria)",
        "    found = Product.objects.filter(**request.GET.dict())",
    ),
    (
        "DJI-006",
        "order by the parameter instead of the mapping's value",
        ICONTROLS,
        '    column = SORTABLE.get(request.GET.get("sort", "name"), "name")',
        '    column = request.GET.get("sort", "name")',
    ),
    (
        "DJI-006",
        "drop the membership test from the checked ordering",
        ICONTROLS,
        '    column = request.GET.get("sort", "name")\n'
        "    if column not in SORTABLE:\n"
        '        column = "name"',
        '    column = request.GET.get("sort", "name")',
    ),
    (
        "DJI-007",
        "deserialise with a loader that executes",
        ICONTROLS,
        '    cart = json.loads(request.POST["cart"])',
        '    cart = pickle.loads(bytes.fromhex(request.POST["cart"]))',
    ),
    (
        "DJI-008",
        "hand the command to a shell as one string",
        ICONTROLS,
        "    subprocess.run(\n"
        '        ["convert", f"/srv/shop/media/{name}", "-resize", "100x100", "/tmp/out.png"],\n'
        "        check=True,\n"
        "    )",
        "    subprocess.run(\n"
        '        f"convert /srv/shop/media/{name} -resize 100x100 /tmp/out.png", shell=True\n'
        "    )",
    ),
    (
        "DJI-009",
        "let the request choose the host rather than the query",
        ICONTROLS,
        "    answer = requests.get("
        "f\"{FEED_HOST}/feeds?{urlencode({'id': request.GET['feed']})}\", timeout=5)",
        '    answer = requests.get(request.GET["feed"], timeout=5)',
    ),
    (
        "DJI-010",
        "redirect without the host check",
        ICONTROLS,
        "    if url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}):\n"
        "        return redirect(target)\n"
        '    return redirect("/")',
        "    return redirect(target)",
    ),
    (
        "DJI-011",
        "mark the unescaped label as trusted HTML",
        ICONTROLS,
        '    return HttpResponse(format_html("<b>{}</b> {}", request.GET["label"], label))',
        "    return HttpResponse(mark_safe(f\"<b>{request.GET['label']}</b> {label}\"))",
    ),
    (
        "DJI-012",
        "open the path directly instead of through storage",
        ICONTROLS,
        '    with default_storage.open(request.GET["file"]) as handle:',
        '    with open(os.path.join("/srv/shop/media", request.GET["file"])) as handle:',
    ),
)

MIGRATION = ROOT / "tests/fixtures/migration_project"
MCONTROL = MIGRATION / "ledger/migrations/0002_entry_attempts.py"

MIGRATION_UNFIXES: tuple[Unfix, ...] = (
    (
        "DJM-009",
        "validate the constraint immediately instead of adding it NOT VALID",
        MCONTROL,
        'AddConstraintNotValid(\n            model_name="entry",',
        'migrations.AddConstraint(\n            model_name="entry",',
    ),
    (
        "DJM-001",
        "drop the default from the added non-nullable column",
        MCONTROL,
        "field=models.IntegerField(default=0),",
        "field=models.IntegerField(),",
    ),
    (
        "DJM-002",
        "narrow the altered column instead of widening it",
        MCONTROL,
        "field=models.CharField(db_index=True, max_length=128),",
        "field=models.CharField(db_index=True, max_length=32),",
    ),
    (
        "DJM-003",
        "build the index in the transaction instead of concurrently",
        MCONTROL,
        "AddIndexConcurrently(",
        "migrations.AddIndex(",
    ),
    (
        "DJM-004",
        "drop the column outright instead of only its database half",
        MCONTROL,
        "migrations.SeparateDatabaseAndState(\n"
        "            database_operations=[migrations.RemoveField("
        'model_name="entry", name="posted")],\n'
        "        ),",
        'migrations.RemoveField(model_name="entry", name="posted"),',
    ),
    (
        "DJM-005",
        "rename the column without pinning it back with db_column",
        MCONTROL,
        'field=models.DateTimeField(auto_now_add=True, db_column="created"),',
        "field=models.DateTimeField(auto_now_add=True),",
    ),
    (
        "DJM-006",
        "drop the reverse from the backfill beside the schema changes",
        MCONTROL,
        "            code=backfill_attempts,\n"
        "            reverse_code=migrations.RunPython.noop,\n",
        "            code=backfill_attempts,\n",
    ),
    (
        "DJM-008",
        "run the schema changes and the backfill in one transaction",
        MCONTROL,
        "\n    atomic = False\n",
        "\n",
    ),
    (
        "DJM-007",
        "fetch the whole table instead of iterating it in chunks",
        MCONTROL,
        "attempts__isnull=True).iterator(chunk_size=500)",
        "attempts__isnull=True)",
    ),
)

PORTABILITY_UNFIXES: tuple[Unfix, ...] = (
    (
        "DJX-003",
        "ask the per-group-latest question with DISTINCT ON instead of a subquery",
        PQUERIES,
        "    newest = (\n"
        '        Item.objects.filter(sku=models.OuterRef("sku")).order_by("-id").values("pk")[:1]\n'
        "    )\n"
        "    return Item.objects.filter(pk__in=models.Subquery(newest))\n",
        '    return Item.objects.order_by("sku", "-id").distinct("sku")\n',
    ),
)

FIXTURES: tuple[tuple[str, Path, tuple[Unfix, ...], str], ...] = (
    ("orm_project", ORM, ORM_UNFIXES, "controls.py"),
    ("injection_project", INJECTION, INJECTION_UNFIXES, "controls.py"),
    # A migration cannot be called controls.py, so this fixture keeps its
    # controls in a whole app instead of a single file.
    ("migration_project", MIGRATION, MIGRATION_UNFIXES, "ledger/"),
    # Nor can a settings module or a models module, so this fixture keeps the
    # controls in a whole app too: `warehouse` is `catalog` written portably.
    ("portability_project", PORTABILITY, PORTABILITY_UNFIXES, "warehouse/"),
)


def findings(fixture: Path) -> list[tuple[str, str, int]]:
    """Every finding as (rule, file, line). Re-read from disk on each call."""
    result = engine.run(fixture, min_severity=Severity.INFO, min_confidence=Confidence.TENTATIVE)
    if result.rule_errors:
        raise SystemExit(f"rules crashed: {result.rule_errors}")
    return [(f.rule_id, f.location.file, f.location.line) for f in result.findings]


def probe(name: str, fixture: Path, unfixes: tuple[Unfix, ...], control: str) -> list[str]:
    """Un-fix every control in one fixture. Returns the failures."""
    baseline = findings(fixture)
    base_keys = set(baseline)
    leaked = [f for f in baseline if control in f[1]]
    assert not leaked, leaked
    print(f"{name}: {len(baseline)} findings, none of them in {control}")

    failures: list[str] = []
    reached: list[tuple[str, str, int]] = []
    for rule_id, label, path, old, new in unfixes:
        original = path.read_text()
        if original.count(old) != 1:
            failures.append(f"UNAPPLIED {rule_id}: {label} -- anchor matched {original.count(old)}")
            continue
        path.write_text(original.replace(old, new, 1))
        try:
            after = findings(fixture)
        finally:
            path.write_text(original)
        gained = sorted(k for k in set(after) - base_keys if k[0] == rule_id)
        if gained:
            reached.extend(gained)
            where = ", ".join(f"{k[1]}:{k[2]}" for k in gained)
            print(f"  REACHED {rule_id:8} {label:56} -> {where}")
        else:
            others = sorted({k[0] for k in set(after) - base_keys})
            failures.append(f"UNREACHED {rule_id}: {label} -- gained {others or 'nothing'} instead")

    # Second job: every line-numbered control in the manifest must still point
    # at the line the un-fix proved the rule speaks on.
    manifest = json.loads((fixture / "expected.json").read_text())
    pinned = {
        (e["rule_id"], e["file"]): e["line"]
        for e in manifest["must_not_report"]
        if e.get("line") is not None
    }
    for rule_id, file, line in sorted(reached):
        if control in file:
            continue
        aimed = pinned.get((rule_id, file))
        if aimed == line:
            print(f"  AIMED    {rule_id:8} {file}:{line}")
        else:
            failures.append(
                f"MISAIMED {rule_id}: manifest forbids {file}:{aimed}, "
                f"but the rule speaks at {file}:{line}"
            )
    for (rule_id, file), line in sorted(pinned.items()):
        if (rule_id, file, line) not in reached:
            failures.append(
                f"UNPROVEN {rule_id}: manifest forbids {file}:{line}, "
                "which no un-fix showed the rule reaching"
            )
    print(f"  {len(unfixes) - len(failures)} of {len(unfixes)} controls proven load-bearing")
    print()
    return failures


def main() -> int:
    failures: list[str] = []
    total = 0
    for name, fixture, unfixes, control in FIXTURES:
        total += len(unfixes)
        failures.extend(probe(name, fixture, unfixes, control))
    for failure in failures:
        print(failure)
    print(f"{total - len(failures)} of {total} controls proven load-bearing across all fixtures")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

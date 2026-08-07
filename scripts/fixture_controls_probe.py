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
FIXTURE = ROOT / "tests/fixtures/orm_project"
CONTROLS = FIXTURE / "inventory/controls.py"
VIEWS = FIXTURE / "inventory/views.py"

UNFIXES: tuple[tuple[str, str, Path, str, str], ...] = (
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


def findings() -> list[tuple[str, str, int]]:
    """Every finding as (rule, file, line). Re-read from disk on each call."""
    result = engine.run(FIXTURE, min_severity=Severity.INFO, min_confidence=Confidence.TENTATIVE)
    if result.rule_errors:
        raise SystemExit(f"rules crashed: {result.rule_errors}")
    return [(f.rule_id, f.location.file, f.location.line) for f in result.findings]


def main() -> int:
    baseline = findings()
    base_keys = set(baseline)
    leaked = [f for f in baseline if "controls.py" in f[1]]
    assert not leaked, leaked
    print(f"baseline: {len(baseline)} findings, none of them in controls.py")

    failures: list[str] = []
    reached: list[tuple[str, str, int]] = []
    for rule_id, label, path, old, new in UNFIXES:
        original = path.read_text()
        if original.count(old) != 1:
            failures.append(f"UNAPPLIED {rule_id}: {label} -- anchor matched {original.count(old)}")
            continue
        path.write_text(original.replace(old, new, 1))
        try:
            after = findings()
        finally:
            path.write_text(original)
        new_keys = set(after) - base_keys
        gained = sorted(k for k in new_keys if k[0] == rule_id)
        if gained:
            reached.extend(gained)
            where = ", ".join(f"{k[1]}:{k[2]}" for k in gained)
            print(f"  REACHED {rule_id:8} {label:56} -> {where}")
        else:
            others = sorted({k[0] for k in new_keys})
            failures.append(f"UNREACHED {rule_id}: {label} -- gained {others or 'nothing'} instead")

    # Second job: every line-numbered control in the manifest must still point
    # at the line the un-fix proved the rule speaks on.
    manifest = json.loads((FIXTURE / "expected.json").read_text())
    pinned = {
        (e["rule_id"], e["file"]): e["line"]
        for e in manifest["must_not_report"]
        if e.get("line") is not None
    }
    print()
    for rule_id, file, line in sorted(reached):
        if file.endswith("controls.py"):
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

    print()
    for failure in failures:
        print(failure)
    print(f"{len(UNFIXES) - len(failures)} of {len(UNFIXES)} controls proven load-bearing")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

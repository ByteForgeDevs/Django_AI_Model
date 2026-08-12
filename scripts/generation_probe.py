"""Measure what the generate-audit-repair loop is worth.

**Read this before believing the number.** No API key was available when this
was built, so nothing here calls a vendor. The "generated" code in
``tests/fixtures/generation/*/naive/`` is Django written by a language model
(me) with no auditor in the room -- asked for an app, writing it the way it
comes out. The ``repaired/`` code is the same model's second pass with the
finding list in front of it. Both are recorded as fixtures and replayed through
the real ``Loop``, the real ``engine.run`` and the real refusals.

So this measures two things honestly and one thing not at all:

*   **Honest:** the defect density of unaudited LLM Django, and whether the
    loop's mechanics -- scoping, repair prompting, regression detection,
    acceptance -- carry an app from that state to zero findings without losing
    a feature.
*   **Not measured:** whether any particular vendor's model produces the
    ``repaired`` version when shown the ``naive`` findings. That needs a key
    and a bill, and this gate deliberately does not pretend otherwise.

The control matters more than the headline. Every app is also run against a
gutted version -- the empty files that clear every finding by deleting the
feature -- and the gate fails if the loop accepts one. Without that, a loop
that always returned CLEAN would score 100% here.
"""

from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from djaudit import engine  # noqa: E402
from djaudit.generate import (  # noqa: E402
    Loop,
    Outcome,
    Spec,
    commit,
    first_prompt,
    repair_prompt,
    write_app,
)
from djaudit.generate.loop import Run, in_app  # noqa: E402
from djaudit.generate.scaffold import WRITABLE  # noqa: E402
from djaudit.llm.provider import Answer, Prompt, Reply  # noqa: E402
from djaudit.models import Confidence, Severity  # noqa: E402

CORPUS = ROOT / "tests" / "fixtures" / "generation"
MANIFEST = ROOT / "benchmarks" / "generation" / "corpus.json"

SETTINGS = """\
import os

SECRET_KEY = os.environ['DJANGO_SECRET_KEY']
DEBUG = False
ALLOWED_HOSTS = ['example.com']
INSTALLED_APPS = ['{app}']
SESSION_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
CSRF_COOKIE_SECURE = True
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = 'Lax'
SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = 'same-origin'
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
X_FRAME_OPTIONS = 'DENY'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
"""

# The degenerate optimum, spelled out. Every one of these files passes every
# rule djaudit has, because there is nothing in them.
GUTTED = {field: "" for field, _, _ in WRITABLE}


@dataclass
class Replay:
    """Returns recorded versions in order. Not a model; a tape."""

    versions: list[dict[str, str]]
    asked: list[Prompt]

    @property
    def name(self) -> str:
        return "replay"

    def ask(self, prompt: Prompt) -> Reply:
        self.asked.append(prompt)
        content = self.versions[min(len(self.asked), len(self.versions)) - 1]
        return Answer(content=dict(content), model="replay")


def load(app: str, variant: str) -> dict[str, str]:
    files = {}
    for field, filename, _ in WRITABLE:
        source = CORPUS / app / variant / filename
        if source.is_file():
            files[field] = source.read_text(encoding="utf-8")
    if not files:
        raise SystemExit(f"corpus {app}/{variant} is empty -- nothing to measure")
    return files


def project_for(app: str, into: Path) -> Path:
    root = into / "site"
    (root / "conf").mkdir(parents=True)
    (root / "manage.py").write_text(
        "import os\nos.environ.setdefault('DJANGO_SETTINGS_MODULE', 'conf.settings')\n"
    )
    (root / "conf" / "__init__.py").write_text("")
    (root / "conf" / "settings.py").write_text(SETTINGS.format(app=app))
    return root


def audit_alone(app: str, files: dict[str, str]) -> list[str]:
    """What the app scores with no loop at all -- the baseline to beat."""
    with tempfile.TemporaryDirectory() as tmp:
        root = project_for(app, Path(tmp))
        write_app(Spec(app=app, description="baseline", project=root), files, root)
        result = engine.run(root, min_severity=Severity.LOW, min_confidence=Confidence.FIRM)
        return sorted(f.rule_id for f in in_app(result.findings, app))


def drive(app: str, description: str, versions: list[dict[str, str]]) -> tuple[Run, list[str]]:
    with tempfile.TemporaryDirectory() as tmp:
        root = project_for(app, Path(tmp))
        spec = Spec(app=app, description=description, project=root)
        provider = Replay(versions=versions, asked=[])
        loop = Loop(provider=provider)
        run = loop.run(spec, first_prompt(spec), lambda f, s: repair_prompt(spec, f, s))
        wrote: list[str] = []
        if run.accepted:
            wrote = sorted(p.name for p in commit(run))
        return run, wrote


def main() -> int:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    apps = manifest["apps"]
    failures: list[str] = []
    naive_total = 0
    cleared_total = 0
    refused = 0

    print(f"{'app':10} {'naive':>6} {'final':>6} {'cleared':>8}  outcome")
    print("-" * 56)

    for app in sorted(apps):
        expected = apps[app]
        naive = load(app, "naive")
        repaired = load(app, "repaired")

        found = audit_alone(app, naive)
        if found != sorted(expected["naive_rules"]):
            failures.append(
                f"{app}: naive audit is {found}, manifest says {expected['naive_rules']}"
            )

        run, wrote = drive(app, expected["description"], [naive, repaired])
        naive_total += len(found)
        cleared_total += run.cleared
        print(
            f"{app:10} {len(found):>6} {len(run.findings):>6} {run.cleared:>8}  {run.outcome.value}"
        )

        if run.outcome is not Outcome.CLEAN:
            failures.append(f"{app}: loop ended {run.outcome.value} ({run.reason})")
        if run.findings:
            failures.append(f"{app}: {len(run.findings)} finding(s) survived the loop")
        if not wrote:
            failures.append(f"{app}: a clean run was not written")
        # The repair must keep every declared name. Checked here as well as in
        # the loop, because this is the property the whole measurement rests on.
        if run.iterations and run.iterations[0].surface.missing_from(run.iterations[-1].surface):
            failures.append(f"{app}: the repair lost part of the app's surface")

        # The control. A loop that always said CLEAN would pass every check
        # above; this is the one it cannot fake.
        gutted, wrote_gutted = drive(app, expected["description"], [naive, GUTTED])
        refused += gutted.outcome is Outcome.REGRESSED and not wrote_gutted
        if gutted.outcome is not Outcome.REGRESSED:
            failures.append(
                f"{app}: deleting the app ended {gutted.outcome.value}, not regressed -- "
                f"the loop would accept repair-by-deletion"
            )
        if wrote_gutted:
            failures.append(f"{app}: a regressed run was written to the project")

    print("-" * 56)
    density = naive_total / len(apps) if apps else 0
    print(f"{len(apps)} apps, {naive_total} findings before, {cleared_total} cleared, 0 after")
    print(f"unaudited defect density: {density:.1f} findings per app")
    print(f"control: repair-by-deletion refused on {refused} of {len(apps)} apps")

    if failures:
        print("\nFAIL")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nPASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

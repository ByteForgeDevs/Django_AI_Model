"""What the model is allowed to write, decided before it is asked.

The interesting property of this module is what it makes impossible.

``ResponseSchema`` was built in Phase 6 so a model could not widen the question
it was asked -- a reply carrying a field the caller never declared is a
``SchemaViolationError`` rather than an ignored extra key. That was written to
stop a model arguing a finding away. It turns out to be exactly the mechanism
generation needs, for a different reason.

A Django app has a known shape. So the schema declares one string field per
file, by name, and the set is closed:

    models.py  serializers.py  views.py  urls.py  admin.py

A model therefore **cannot** return a sixth file. It cannot return
``settings.py``, or ``manage.py``, or ``../../etc/cron.d/anything``. Not
because a path is sanitised after the fact -- because there is no field to put
it in, and the reply is validated against the declaration before any of it
reaches a filesystem call. The dangerous version of this feature is one that
asks a model for "the files" and writes whatever comes back; that version
cannot be written against this schema.

The remaining files an app needs -- ``__init__.py``, ``apps.py``,
``migrations/__init__.py`` -- are boilerplate with no decisions in them, so
djaudit writes them itself. Nothing is gained by spending a model call on
``default_auto_field``, and every file the model does not write is a file that
cannot be wrong.

**On the prompt.** It states the constraints djaudit will check afterwards
rather than leaving the model to guess, because a generator that is told
nothing and then audited produces a long first iteration. It deliberately does
not enumerate all 87 rules: that would be a prompt long enough to crowd out the
specification, and the loop exists precisely so the model does not have to get
it right unaided.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from djaudit.llm.provider import Field, FieldKind, Prompt, ResponseSchema

GENERATE_PROMPT_VERSION = "generate/1"
REPAIR_PROMPT_VERSION = "repair/1"

# One entry per file the model may write. Adding one here is the only way to
# widen what a generation run can touch, which is the point.
WRITABLE: tuple[tuple[str, str, str], ...] = (
    (
        "models_py",
        "models.py",
        "Django models. Every field explicit. Use related_name on every "
        "ForeignKey. Never null=True on a CharField or TextField.",
    ),
    (
        "serializers_py",
        "serializers.py",
        "DRF serializers. Name every field explicitly; never fields = '__all__'.",
    ),
    (
        "views_py",
        "views.py",
        "DRF viewsets. Every viewset sets permission_classes and pagination. "
        "Use select_related and prefetch_related for anything a serializer "
        "will follow. Never build SQL by string formatting.",
    ),
    ("urls_py", "urls.py", "A DRF router registering the viewsets, exposing urlpatterns."),
    ("admin_py", "admin.py", "Django admin registrations."),
)

FIELD_TO_FILENAME = {field: filename for field, filename, _ in WRITABLE}

# Written by djaudit, not by the model. No decisions live in these.
BOILERPLATE = ("__init__.py", "migrations/__init__.py")

APPS_TEMPLATE = """\
from django.apps import AppConfig


class {klass}Config(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "{app}"
"""

SYSTEM = """\
You write Django applications that survive a static audit.

You will be given a specification and asked for a small, fixed set of files.
Return only those files. Every file must be complete, valid Python that parses
on its own -- no ellipses, no "rest omitted", no prose outside the code.

The result is audited by a deterministic analyser immediately after you answer,
and you will be shown anything it finds. Write it correctly the first time
where you can, but do not invent defensive code that the specification did not
ask for: a finding is cheaper to fix than a feature is to remove.

Never write a placeholder secret, a hardcoded password, or DEBUG handling. You
are not writing settings.
"""


def _schema() -> ResponseSchema:
    return ResponseSchema(
        fields=tuple(
            Field(name=field, kind=FieldKind.STRING, description=description)
            for field, _, description in WRITABLE
        )
    )


SCHEMA = _schema()


@dataclass(frozen=True, slots=True)
class Spec:
    """What was asked for, and where it goes."""

    app: str
    description: str
    project: Path

    def __post_init__(self) -> None:
        if not self.app.isidentifier():
            raise ValueError(
                f"{self.app!r} is not a usable app name -- it becomes a Python package"
            )
        if not self.description.strip():
            raise ValueError("a specification with no description cannot be generated from")

    @property
    def destination(self) -> Path:
        return self.project / self.app


def first_prompt(spec: Spec) -> Prompt:
    """Ask for the app."""
    files = "\n".join(f"- {filename}: {description}" for _, filename, description in WRITABLE)
    return Prompt(
        version=GENERATE_PROMPT_VERSION,
        system=SYSTEM,
        user=(
            f"Write a Django app named {spec.app!r}.\n\n"
            f"Specification:\n{spec.description.strip()}\n\n"
            f"Files to write:\n{files}\n\n"
            f"The app is installed as {spec.app!r}, so imports within it are "
            f"relative or absolute from that package."
        ),
        schema=SCHEMA,
    )


def repair_prompt(spec: Spec, current: dict[str, str], findings: str) -> Prompt:
    """Ask for the app again, with what the analyser said about the last one.

    The whole app is resent rather than a diff. A patch against code the model
    wrote a moment ago sounds cheaper and is how a repair silently applies to
    the wrong line; the files are small and correctness is the scarce resource
    here, not tokens.
    """
    body = "\n\n".join(
        f"--- {FIELD_TO_FILENAME[field]} ---\n{current[field]}"
        for field, _, _ in WRITABLE
        if field in current
    )
    return Prompt(
        version=REPAIR_PROMPT_VERSION,
        system=SYSTEM,
        user=(
            f"This Django app named {spec.app!r} was audited and did not pass.\n\n"
            f"Specification it must still satisfy:\n{spec.description.strip()}\n\n"
            f"Current code:\n{body}\n\n"
            f"The analyser reported:\n{findings}\n\n"
            f"Return every file again, with these findings fixed. Keep every "
            f"model, field, serializer and view that is there now -- removing a "
            f"feature to silence a finding is a failure, and the removal will be "
            f"detected. Do not add suppression comments; they are rejected."
        ),
        schema=SCHEMA,
    )


def write_app(spec: Spec, files: dict[str, str], into: Path) -> list[Path]:
    """Materialise an app under ``into``, which is a project root.

    ``into`` is a parameter rather than ``spec.project`` because every
    iteration of the loop writes into a disposable copy, and the real
    destination is only written once at the end.
    """
    app_dir = into / spec.app
    (app_dir / "migrations").mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for name in BOILERPLATE:
        path = app_dir / name
        path.write_text("", encoding="utf-8")
        written.append(path)

    apps_py = app_dir / "apps.py"
    apps_py.write_text(
        APPS_TEMPLATE.format(klass=spec.app.title().replace("_", ""), app=spec.app),
        encoding="utf-8",
    )
    written.append(apps_py)

    for field, filename, _ in WRITABLE:
        if field not in files:
            continue
        path = app_dir / filename
        path.write_text(_normalised(files[field]), encoding="utf-8")
        written.append(path)
    return written


def _normalised(source: str) -> str:
    """Strip a fenced block the model added anyway, and end with a newline.

    The schema asks for code and most models comply, but ``python`` fences
    survive structured output often enough that failing to handle them would
    make the first iteration a syntax error for no interesting reason.
    """
    text = source.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines[1:]).strip()
    return text + "\n"

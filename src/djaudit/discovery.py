"""Locating the Django project inside an arbitrary checkout.

Everything here is static-tier: we read and parse files, we never import or
execute the target. That keeps djaudit safe to point at untrusted code and
usable without installing the target's dependencies.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path

from djaudit.astutils import literal, module_assignments, star_import_targets
from djaudit.context import Diagnostic, ProjectContext, SettingsModule, SettingsRole

EXCLUDED_DIR_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".tox",
        ".nox",
        ".eggs",
        ".idea",
        ".vscode",
        "node_modules",
        "site-packages",
        "build",
        "dist",
        "htmlcov",
        ".venv",
        "venv",
    }
)

SETTINGS_PARENT_DIRS = frozenset({"settings", "conf", "config"})

SETTINGS_MARKERS = frozenset(
    {
        "INSTALLED_APPS",
        "DATABASES",
        "SECRET_KEY",
        "MIDDLEWARE",
        "MIDDLEWARE_CLASSES",
        "ROOT_URLCONF",
        "ALLOWED_HOSTS",
        "TEMPLATES",
        "WSGI_APPLICATION",
        "ASGI_APPLICATION",
        "DEFAULT_AUTO_FIELD",
        "AUTH_PASSWORD_VALIDATORS",
        "STATIC_URL",
        "USE_TZ",
    }
)

_ROLE_KEYWORDS: tuple[tuple[SettingsRole, frozenset[str]], ...] = (
    (SettingsRole.TEST, frozenset({"test", "tests", "testing", "testutils", "ci", "pytest"})),
    (SettingsRole.DEVELOPMENT, frozenset({"development", "dev", "local", "localhost", "debug"})),
    (
        SettingsRole.PRODUCTION,
        frozenset({"production", "prod", "live", "staging", "stage", "deploy", "heroku"}),
    ),
    (SettingsRole.BASE, frozenset({"base", "common", "defaults", "shared"})),
)

_DJANGO_PIN = re.compile(
    r"^\s*django(?:\[[^\]]*\])?\s*[=><~!]{1,2}\s*[\"']?([0-9][0-9A-Za-z.\-]*)",
    re.IGNORECASE | re.MULTILINE,
)


def _is_virtualenv(path: Path) -> bool:
    return (path / "pyvenv.cfg").is_file()


def iter_python_files(root: Path) -> Iterator[Path]:
    """Yield every ``.py`` file under ``root``, skipping caches and virtualenvs.

    Virtualenvs are detected by ``pyvenv.cfg`` rather than by directory name, so
    a legitimately named application package is never mistaken for one.
    """
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except (OSError, PermissionError):
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if entry.name in EXCLUDED_DIR_NAMES or _is_virtualenv(entry):
                    continue
                stack.append(entry)
            elif entry.suffix == ".py":
                yield entry


def dotted_path(root: Path, path: Path) -> str:
    """Convert a file path into its Python dotted module name."""
    try:
        relative = path.resolve().relative_to(root.resolve())
    except ValueError:
        relative = path
    parts = list(relative.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _tokens(name: str) -> set[str]:
    return set(re.split(r"[_\-.]", name.lower()))


def classify_settings_role(path: Path, root: Path | None = None) -> SettingsRole:
    """Infer what a settings module is for from where it lives.

    Deliberately keyword-based rather than clever. The cost of guessing wrong is
    asymmetric: mislabelling production as development hides real findings, so
    anything unrecognised falls through to ``UNKNOWN``, which still counts as
    production-reaching.

    The filename decides when it says anything, because it is the more specific
    signal -- ``tests/production.py`` is still about production. Only when the
    name is silent do the directories get a say, and then only to identify test
    and development trees. pretix keeps its test settings in
    ``pretix/testutils/settings.py``, whose stem reads as an ordinary primary
    settings module; without looking at the directory, its deliberate
    ``DEBUG = True`` and MD5 password hashers are reported as critical
    findings, which is precisely the noise that gets a tool uninstalled.

    Directories are only considered *below* ``root``. A checkout that happens
    to sit in ``/home/me/test/`` must not have every settings module in it
    silently downgraded.
    """
    stem = path.stem.lower()
    if stem == "__init__":
        stem = path.parent.name.lower()
    tokens = _tokens(stem)
    for role, keywords in _ROLE_KEYWORDS:
        if tokens & keywords:
            return role

    inferred = _role_from_directories(path, root)
    if inferred is not None:
        return inferred
    if stem == "settings":
        return SettingsRole.PRIMARY
    return SettingsRole.UNKNOWN


def _role_from_directories(path: Path, root: Path | None) -> SettingsRole | None:
    """Test or development trees, read from the directories below ``root``.

    Restricted to those two roles on purpose. They are the ones that suppress a
    finding, so they are the only ones worth inferring from weaker evidence --
    and inferring anything else from a directory name would change nothing.
    """
    if root is None:
        return None
    try:
        relative = path.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    directories = {token for part in relative.parts[:-1] for token in _tokens(part)}
    for role, keywords in _ROLE_KEYWORDS:
        if role in (SettingsRole.TEST, SettingsRole.DEVELOPMENT) and directories & keywords:
            return role
    return None


def find_manage_py(files: tuple[Path, ...]) -> Path | None:
    """The shallowest ``manage.py``, which is almost always the project's own."""
    candidates = [f for f in files if f.name == "manage.py"]
    if not candidates:
        return None
    return min(candidates, key=lambda p: (len(p.parts), str(p)))


def settings_module_from_manage(ctx: ProjectContext, manage_py: Path) -> str | None:
    """Read ``DJANGO_SETTINGS_MODULE`` out of ``manage.py`` without running it."""
    tree = ctx.parse(manage_py)
    if tree is None:
        return None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in ("setdefault", "__setitem__"):
            continue
        if len(node.args) != 2:
            continue
        key = literal(node.args[0])
        if key == "DJANGO_SETTINGS_MODULE":
            value = literal(node.args[1])
            if isinstance(value, str):
                return value
    return None


def resolve_dotted(root: Path, dotted: str) -> Path | None:
    """Map a dotted module path to a file inside ``root``."""
    relative = Path(*dotted.split("."))
    for candidate in (root / relative.with_suffix(".py"), root / relative / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def locate_module(ctx: ProjectContext, dotted: str) -> Path | None:
    """The file a dotted module name refers to, inside the analysed tree.

    Not simply ``root / dotted``: a project's importable root is often a
    directory below the repository root -- NetBox's ``netbox.urls`` lives at
    ``netbox/netbox/urls.py`` -- so the name is matched as a suffix of the
    paths we already found, nearest the root winning.
    """
    if not dotted:
        return None
    relative = Path(*dotted.split("."))
    wanted = (relative.with_suffix(".py").as_posix(), (relative / "__init__.py").as_posix())
    matches = [
        candidate
        for candidate in ctx.python_files
        for target in wanted
        if candidate.as_posix() == (ctx.root / target).as_posix()
        or candidate.as_posix().endswith("/" + target)
    ]
    return min(matches, key=lambda p: (len(p.parts), p.as_posix())) if matches else None


def _is_settings_candidate(path: Path) -> bool:
    return path.stem == "settings" or path.parent.name in SETTINGS_PARENT_DIRS


def _defines_settings_markers(ctx: ProjectContext, path: Path) -> bool:
    tree = ctx.parse(path)
    if tree is None:
        return False
    return any(a.name in SETTINGS_MARKERS for a in module_assignments(tree))


def discover_settings_modules(
    ctx: ProjectContext, files: tuple[Path, ...], entrypoint: str | None
) -> tuple[SettingsModule, ...]:
    """Find the project's settings modules.

    Two passes. First, name-based candidates are confirmed by checking they
    actually assign recognisable Django settings -- this stops an unrelated
    ``config/settings.py`` in some vendored library from being audited as if it
    configured the project. Second, candidates that ``import *`` from a confirmed
    module are pulled in too, which is how split-settings layouts hang together.
    """
    candidates = [p for p in files if _is_settings_candidate(p)]
    if entrypoint:
        resolved = resolve_dotted(ctx.root, entrypoint)
        if resolved is not None and resolved not in candidates:
            candidates.append(resolved)

    confirmed = {p for p in candidates if _defines_settings_markers(ctx, p)}
    confirmed_dotted = {dotted_path(ctx.root, p) for p in confirmed}

    for path in candidates:
        if path in confirmed:
            continue
        tree = ctx.parse(path)
        if tree is None:
            continue
        for target in star_import_targets(tree):
            absolute = target.lstrip(".")
            if any(d == absolute or d.endswith(f".{absolute}") for d in confirmed_dotted):
                confirmed.add(path)
                break

    entry_path = resolve_dotted(ctx.root, entrypoint) if entrypoint else None
    return tuple(
        sorted(
            (
                SettingsModule(
                    path=path,
                    dotted=dotted_path(ctx.root, path),
                    role=classify_settings_role(path, ctx.root),
                    is_entrypoint=(entry_path is not None and path == entry_path),
                )
                for path in confirmed
            ),
            key=lambda m: m.dotted,
        )
    )


def _class_settings_markers(ctx: ProjectContext, path: Path) -> tuple[str, ...]:
    """Settings assigned inside a class body rather than at module level.

    ``django-configurations`` (and a few hand-rolled equivalents) puts the whole
    configuration in class attributes, so ``module_assignments`` sees an empty
    module and the file is never confirmed as settings. Detecting the shape does
    not analyse it, but it turns "found nothing" into "found something I cannot
    read yet", which is the difference between a silent pass and a useful one.
    """
    tree = ctx.parse(path)
    if tree is None:
        return ()
    found: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        for stmt in node.body:
            targets: list[ast.expr] = []
            if isinstance(stmt, ast.Assign):
                targets = list(stmt.targets)
            elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
                targets = [stmt.target]
            found.update(
                t.id for t in targets if isinstance(t, ast.Name) and t.id in SETTINGS_MARKERS
            )
    return tuple(sorted(found))


def diagnose_settings(ctx: ProjectContext, files: tuple[Path, ...]) -> tuple[Diagnostic, ...]:
    """Report when a project that clearly has settings yielded none.

    Every ``DJS`` rule reads a settings module, so finding none silently turns a
    whole family off and the run exits 0 having audited nothing. That is the
    worst failure mode available to this tool -- worse than a crash, because a
    crash is noticed. It is only raised when the checkout actually looks like a
    Django project: pointing djaudit at a reusable app, which legitimately has
    no settings, must stay quiet.
    """
    if ctx.settings_modules:
        return ()

    candidates = [p for p in files if _is_settings_candidate(p)]
    if ctx.manage_py is None and not candidates:
        return ()

    class_based = [(p, m) for p in candidates for m in (_class_settings_markers(ctx, p),) if m]
    if class_based:
        path, markers = class_based[0]
        others = f" (and {len(class_based) - 1} more)" if len(class_based) > 1 else ""
        return (
            Diagnostic(
                code="settings-in-class-body",
                message="No settings module could be read: this project keeps its settings "
                "in class attributes.",
                detail=f"{ctx.rel(path)} assigns {', '.join(markers[:3])} inside a class "
                f"body{others}, which is how django-configurations works. djaudit only "
                "reads module-level assignments today, so every DJS rule was skipped and "
                "this result says nothing about the project's security. Support is "
                "planned; until then, audit the module that django-configurations "
                "generates, or set DJANGO_SETTINGS_MODULE to a plain settings module.",
            ),
        )

    return (
        Diagnostic(
            code="no-settings-module",
            message="No settings module could be found, so no settings were audited.",
            detail="A manage.py or settings-shaped file is present, but nothing assigns "
            "recognisable Django settings at module level. Every DJS rule was skipped, so "
            "a clean result here means only that djaudit found nothing to look at. Pass "
            "the project root, or check that the settings module is not excluded.",
        ),
    )


def detect_django_version(root: Path) -> str | None:
    """Read the pinned Django version from dependency manifests.

    Rules gate on this: behaviour and defaults differ between 5.2 LTS and 6.0,
    and reporting a 6.0-only issue against a 5.2 project is a false positive.
    """
    manifests: list[Path] = []
    for pattern in ("requirements*.txt", "pyproject.toml", "setup.py", "Pipfile"):
        manifests.extend(sorted(root.glob(pattern)))
    requirements_dir = root / "requirements"
    if requirements_dir.is_dir():
        manifests.extend(sorted(requirements_dir.glob("*.txt")))

    for manifest in manifests:
        try:
            text = manifest.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        match = _DJANGO_PIN.search(text)
        if match:
            return match.group(1)
    return None


def build_context(root: Path) -> ProjectContext:
    """Discover everything the static tier needs about a checkout."""
    root = root.resolve()
    ctx = ProjectContext(root=root)
    files = tuple(sorted(iter_python_files(root)))
    ctx.python_files = files
    ctx.manage_py = find_manage_py(files)
    ctx.settings_entrypoint = (
        settings_module_from_manage(ctx, ctx.manage_py) if ctx.manage_py else None
    )
    ctx.settings_modules = discover_settings_modules(ctx, files, ctx.settings_entrypoint)
    ctx.django_version = detect_django_version(root)
    ctx.diagnostics = diagnose_settings(ctx, files)
    return ctx

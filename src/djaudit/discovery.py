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
from djaudit.context import ProjectContext, SettingsModule, SettingsRole

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
    (SettingsRole.TEST, frozenset({"test", "tests", "testing", "ci", "pytest"})),
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


def classify_settings_role(path: Path) -> SettingsRole:
    """Infer what a settings module is for from its filename.

    Deliberately keyword-based rather than clever. The cost of guessing wrong is
    asymmetric: mislabelling production as development hides real findings, so
    anything unrecognised falls through to ``UNKNOWN``, which still counts as
    production-reaching.
    """
    stem = path.stem.lower()
    if stem == "__init__":
        stem = path.parent.name.lower()
    tokens = set(re.split(r"[_\-.]", stem))
    for role, keywords in _ROLE_KEYWORDS:
        if tokens & keywords:
            return role
    if stem == "settings":
        return SettingsRole.PRIMARY
    return SettingsRole.UNKNOWN


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
                    role=classify_settings_role(path),
                    is_entrypoint=(entry_path is not None and path == entry_path),
                )
                for path in confirmed
            ),
            key=lambda m: m.dotted,
        )
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
    return ctx

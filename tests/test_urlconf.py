"""Reading urlconfs.

Phase 1 asks this only where the admin is, but the DJA family in Phase 2 is
entirely routes and views, so the reader is pinned properly now rather than
after rules have grown around its quirks.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from djaudit.discovery import build_context, locate_module
from djaudit.urlconf import pattern_of, routes


def parse(source: str) -> ast.expr:
    expression = ast.parse(source, mode="eval").body
    return expression


@pytest.mark.parametrize(
    ("source", "text", "literal"),
    [
        ("'admin/'", "admin/", True),
        ("f'admin/'", "admin/", True),
        ("f'{prefix}admin/'", "admin/", False),
        ("'api/' + 'v1/'", "api/v1/", True),
        ("'api/' + version", "api/", False),
        ("PREFIX", "", False),
        ("b'admin/'", "", False),
    ],
)
def test_patterns_are_read_as_far_as_they_are_knowable(
    source: str, text: str, literal: bool
) -> None:
    assert pattern_of(parse(source)) == (text, literal)


def project(tmp_path: pathlib.Path, urls: str) -> pathlib.Path:
    root = tmp_path / "proj"
    (root / "config").mkdir(parents=True)
    (root / "manage.py").write_text(
        "import os\nos.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')\n"
    )
    (root / "config" / "__init__.py").write_text("")
    (root / "config" / "settings.py").write_text(
        'DATABASES = {}\nSECRET_KEY = "x"\nROOT_URLCONF = "config.urls"\n'
    )
    (root / "config" / "urls.py").write_text(urls)
    return root


def test_every_router_spelling_is_read(tmp_path: pathlib.Path) -> None:
    root = project(
        tmp_path,
        "urlpatterns = [\n"
        "    path('a/', views.a),\n"
        "    re_path(r'^b/$', views.b),\n"
        "    url(r'^c/$', views.c),\n"
        "    django.urls.path('d/', views.d),\n"
        "]\n",
    )
    ctx = build_context(root)
    found = routes(ctx, root / "config" / "urls.py")
    assert [r.router for r in found] == ["path", "re_path", "url", "path"]
    assert [r.view for r in found] == ["views.a", "views.b", "views.c", "views.d"]


def test_routes_outside_urlpatterns_still_count(tmp_path: pathlib.Path) -> None:
    # Real urlconfs build the list by concatenation, in branches, and through
    # helpers. A route written in a file Django imports is a route.
    root = project(
        tmp_path,
        "urlpatterns = [path('a/', views.a)]\n"
        "if settings.DEBUG:\n"
        "    urlpatterns += [path('__debug__/', include('debug_toolbar.urls'))]\n"
        "def extra():\n"
        "    return [path('b/', views.b)]\n",
    )
    ctx = build_context(root)
    assert len(routes(ctx, root / "config" / "urls.py")) == 3


def test_a_single_argument_call_is_not_a_route(tmp_path: pathlib.Path) -> None:
    root = project(tmp_path, "urlpatterns = [path('a/')]\n")
    ctx = build_context(root)
    assert routes(ctx, root / "config" / "urls.py") == ()


def test_an_unparseable_file_yields_nothing(tmp_path: pathlib.Path) -> None:
    root = project(tmp_path, "urlpatterns = [path(\n")
    ctx = build_context(root)
    assert routes(ctx, root / "config" / "urls.py") == ()


def test_a_module_below_the_repository_root_is_found(tmp_path: pathlib.Path) -> None:
    # NetBox's shape: netbox.urls lives at netbox/netbox/urls.py, so the dotted
    # name is a suffix of the path rather than the whole of it.
    root = tmp_path / "repo"
    (root / "netbox" / "netbox").mkdir(parents=True)
    (root / "netbox" / "manage.py").write_text(
        "import os\nos.environ.setdefault('DJANGO_SETTINGS_MODULE', 'netbox.settings')\n"
    )
    (root / "netbox" / "netbox" / "__init__.py").write_text("")
    (root / "netbox" / "netbox" / "settings.py").write_text('DATABASES = {}\nSECRET_KEY = "x"\n')
    (root / "netbox" / "netbox" / "urls.py").write_text("urlpatterns = []\n")
    ctx = build_context(root)
    assert locate_module(ctx, "netbox.urls") == root / "netbox" / "netbox" / "urls.py"


def test_a_package_urlconf_is_found(tmp_path: pathlib.Path) -> None:
    root = project(tmp_path, "urlpatterns = []\n")
    (root / "config" / "urls.py").unlink()
    (root / "config" / "urls").mkdir()
    (root / "config" / "urls" / "__init__.py").write_text("urlpatterns = []\n")
    ctx = build_context(root)
    assert locate_module(ctx, "config.urls") == root / "config" / "urls" / "__init__.py"


def test_an_unknown_module_resolves_to_nothing(tmp_path: pathlib.Path) -> None:
    ctx = build_context(project(tmp_path, "urlpatterns = []\n"))
    assert locate_module(ctx, "nowhere.urls") is None
    assert locate_module(ctx, "") is None

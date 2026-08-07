"""`DJI-012` -- a file opened at a path the request supplies.

The cases that carry the design are the ones about *which name* is a sink:
``open`` bare but never attributed, and ``remove`` attributed to ``os`` but
never bare, each measured against a corpus that spells both the other way.
"""

from __future__ import annotations

import ast
import textwrap

from djaudit.context import ProjectContext
from djaudit.models import Confidence, EvidenceKind, Family, Finding, Severity
from djaudit.registry import all_rules


def run(ctx: ProjectContext) -> list[Finding]:
    rule = next(r for r in all_rules() if r.meta.id == "DJI-012")
    return list(rule().check(ctx))


def view(body: str, extra: str = "") -> dict[str, str]:
    """A project holding one view, which is where a request is in scope."""
    return {
        "manage.py": "import os\n",
        "app/views.py": textwrap.dedent(
            """
            import os
            import shutil
            import copy
            import tarfile
            from pathlib import Path
            from PIL import Image
            from django.core.files.storage import default_storage
            from django.utils._os import safe_join

            BASE = "/var/data"
            """
        )
        + textwrap.dedent(extra)
        + textwrap.dedent(
            """

            def download(request):
            """
        )
        + textwrap.indent(textwrap.dedent(body), "    "),
    }


class TestWhenTheRequestChoosesTheFile:
    def test_a_request_read_opened(self, make_project):
        body = 'return open(request.GET["f"])\n'
        assert len(run(make_project(view(body)))) == 1

    def test_a_local_holding_a_request_read(self, make_project):
        body = 'name = request.GET["f"]\nreturn open(name)\n'
        assert len(run(make_project(view(body)))) == 1

    def test_a_join_onto_a_constant_base(self, make_project):
        """An absolute payload makes join discard the base entirely."""
        body = 'return open(os.path.join(BASE, request.GET["f"]))\n'
        assert len(run(make_project(view(body)))) == 1

    def test_a_pathlib_division(self, make_project):
        body = 'return open(Path(BASE) / request.GET["f"])\n'
        assert len(run(make_project(view(body)))) == 1

    def test_concatenation_onto_a_constant_base(self, make_project):
        """Unlike a URL host, a base directory can be climbed out of."""
        body = 'return open(BASE + request.GET["f"])\n'
        assert len(run(make_project(view(body)))) == 1

    def test_an_f_string(self, make_project):
        body = "return open(f'/var/data/{request.GET[\"f\"]}')\n"
        assert len(run(make_project(view(body)))) == 1

    def test_percent_formatting(self, make_project):
        body = 'return open("/var/data/%s" % request.GET["f"])\n'
        assert len(run(make_project(view(body)))) == 1

    def test_a_trailing_constant_does_not_help(self, make_project):
        body = 'return open(BASE + request.GET["f"] + ".pdf")\n'
        assert len(run(make_project(view(body)))) == 1

    def test_removal(self, make_project):
        body = 'os.remove(os.path.join(BASE, request.GET["f"]))\n'
        assert len(run(make_project(view(body)))) == 1

    def test_both_ends_of_a_move(self, make_project):
        source = 'shutil.move(request.GET["f"], BASE)\n'
        target = 'shutil.move(BASE, request.GET["f"])\n'
        assert len(run(make_project(view(source)))) == 1
        assert len(run(make_project(view(target)))) == 1


class TestWhatDjangoAlreadyRefuses:
    """FileSystemStorage resolves names with safe_join and raises."""

    def test_the_storage_api_is_not_reported(self, make_project):
        body = 'return default_storage.open(request.GET["f"])\n'
        assert run(make_project(view(body))) == []

    def test_the_contrast_with_the_builtin(self, make_project):
        """Silence above is about the holder, not about the argument."""
        through_storage = 'return default_storage.open(request.GET["f"])\n'
        through_builtin = 'return open(request.GET["f"])\n'
        assert run(make_project(view(through_storage))) == []
        assert len(run(make_project(view(through_builtin)))) == 1


class TestWhichNamesAreTrusted:
    """209 corpus calls are named like a sink and one of them is one."""

    def test_an_image_library_open(self, make_project):
        body = 'return Image.open(request.GET["f"])\n'
        assert run(make_project(view(body))) == []

    def test_an_archive_open(self, make_project):
        body = 'return tarfile.open(request.GET["f"])\n'
        assert run(make_project(view(body))) == []

    def test_removing_from_a_list(self, make_project):
        """``parts.remove(x)`` is 34 corpus calls and touches no file."""
        body = 'parts = []\nparts.remove(request.GET["f"])\n'
        assert run(make_project(view(body))) == []

    def test_the_copy_module(self, make_project):
        """``copy.copy`` is 80 corpus calls and is not ``shutil.copy``."""
        body = 'return copy.copy(request.GET["f"])\n'
        assert run(make_project(view(body))) == []

    def test_the_contrast_with_os_remove(self, make_project):
        """Silence above is about the holder, not about the argument."""
        on_a_list = 'parts = []\nparts.remove(request.GET["f"])\n'
        on_the_module = 'os.remove(request.GET["f"])\n'
        assert run(make_project(view(on_a_list))) == []
        assert len(run(make_project(view(on_the_module)))) == 1


class TestWhenTheProjectBuildsThePath:
    def test_a_basename_call(self, make_project):
        body = 'return open(os.path.join(BASE, os.path.basename(request.GET["f"])))\n'
        assert run(make_project(view(body))) == []

    def test_a_safe_join(self, make_project):
        body = 'return open(safe_join(BASE, request.GET["f"]))\n'
        assert run(make_project(view(body))) == []

    def test_a_string_join_is_not_a_path_join(self, make_project):
        """``os.path.join`` splices a path; ``",".join`` does not."""
        body = 'return open(",".join(request.GET.getlist("f")))\n'
        assert run(make_project(view(body))) == []

    def test_a_method_the_project_wrote(self, make_project):
        body = 'return open(builder.compose(request.GET["f"]))\n'
        assert run(make_project(view(body))) == []

    def test_a_helper_the_project_wrote(self, make_project):
        extra = """
            def pick(name):
                return os.path.join(BASE, name)
            """
        body = 'return open(pick(request.GET["f"]))\n'
        assert run(make_project(view(body, extra))) == []


class TestHowItSpeaks:
    def test_the_finding_names_the_source_and_the_sink(self, make_project):
        body = 'return open(os.path.join(BASE, request.GET["f"]))\n'
        (finding,) = run(make_project(view(body)))
        assert finding.rule_id == "DJI-012"
        assert finding.family is Family.DJI
        assert finding.severity is Severity.HIGH
        assert finding.confidence is Confidence.CERTAIN
        assert "request.GET" in finding.message
        assert finding.properties["sink"] == "open"
        assert {evidence.kind for evidence in finding.evidence} == {EvidenceKind.AST}

    def test_a_module_sink_is_named_with_its_module(self, make_project):
        body = 'os.remove(request.GET["f"])\n'
        (finding,) = run(make_project(view(body)))
        assert finding.properties["sink"] == "os.remove"


class TestWithNothingToReasonFrom:
    def test_a_project_with_no_python(self, make_project):
        assert run(make_project({"manage.py": "import os\n"})) == []

    def test_a_constant_path(self, make_project):
        assert run(make_project(view('return open("/etc/hosts")\n'))) == []

    def test_a_file_that_touches_nothing(self, make_project):
        assert run(make_project(view('return request.GET["f"]\n'))) == []

    def test_accesses_returns_nothing_for_an_unrelated_call(self):
        from djaudit.rules.traversal import accesses

        node = ast.parse("json.dumps(x)").body[0]
        assert isinstance(node, ast.Expr)
        assert isinstance(node.value, ast.Call)
        assert list(accesses(node.value)) == []

    def test_a_sink_with_no_arguments(self, make_project):
        assert run(make_project(view("return open()\n"))) == []

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


class TestWhichExpressionIsBlamed:
    """The walk has to name the tainted *part*, not the expression containing it.

    Every test above asserts that a finding exists. Mutation showed that is not
    enough: replacing each descent through a composition with a bare taint test
    on the whole node left all of them passing, because a composed expression
    containing request data is itself tainted, so the rule still reported --
    just about a coarser node. Six mutants survived that way.

    It matters to the reader, not only to the probe. The message and the second
    evidence excerpt are the two places a user learns *what* the client
    controls, and `BASE + request.GET["f"]` names a constant they can ignore
    where `request.GET["f"]` names the hole.
    """

    def blamed(self, project) -> str:
        found = run(project)
        assert len(found) == 1, found
        excerpt = next(e for e in found[0].evidence if e.kind is EvidenceKind.AST)
        assert excerpt.content
        return found[0].evidence[1].content

    def test_concatenation_blames_the_added_part(self, make_project):
        body = 'return open(BASE + request.GET["f"])\n'
        assert self.blamed(make_project(view(body))) == "request.GET['f']"

    def test_an_fstring_blames_the_interpolation(self, make_project):
        body = "return open(f\"{BASE}/{request.GET['f']}\")\n"
        assert self.blamed(make_project(view(body))) == "request.GET['f']"

    def test_percent_interpolation_blames_the_argument(self, make_project):
        body = 'return open("%s/%s" % (BASE, request.GET["f"]))\n'
        assert self.blamed(make_project(view(body))) == "request.GET['f']"

    def test_a_join_blames_the_supplied_argument(self, make_project):
        body = 'return open(os.path.join(BASE, request.GET["f"]))\n'
        assert self.blamed(make_project(view(body))) == "request.GET['f']"

    def test_pathlib_division_blames_the_divisor(self, make_project):
        body = 'return open(Path(BASE) / request.GET["f"])\n'
        assert self.blamed(make_project(view(body))) == "request.GET['f']"

    def test_a_name_is_resolved_to_what_it_holds(self, make_project):
        """Blaming the local would tell the reader nothing they did not write."""
        body = 'name = request.GET["f"]\nreturn open(BASE + name)\n'
        assert self.blamed(make_project(view(body))) == "request.GET['f']"

    def test_a_name_defined_from_itself_still_blames_the_request(self, make_project):
        """The recursion guard, and the only shape that reaches it.

        Resolving `name` walks to its definitions, one of which reads `name`
        again. The guard stops there and lets the other definition answer. A
        mutant that instead returned the repeated name when it happened to be
        tainted still produced a finding -- on the same line, with the same
        rule -- and blamed `name`, which tells the reader nothing. Asserting
        the finding exists could never have caught that.
        """
        body = 'name = request.GET["f"]\nname = BASE + name\nreturn open(name)\n'
        assert self.blamed(make_project(view(body))) == "request.GET['f']"


class TestWhichHolderOwnsTheVerb:
    """`copy` and `move` are ordinary words, and only two modules make them sinks.

    Without this, the mutants that drop the `os` and `shutil` restrictions
    survive: nothing in the suite calls a same-named method on anything else,
    so the guard looked untested and, to a reader, unnecessary.
    """

    def test_a_project_object_with_a_filesystem_name_is_not_a_sink(self, make_project):
        body = 'return copy.copy(request.GET["f"])\n'
        assert run(make_project(view(body))) == []

    def test_an_os_verb_on_shutil_is_not_a_sink(self, make_project):
        """The cross product, which is where the two guards are actually tested.

        `copy.copy` never reaches them: the holder is not in `MODULES`, so the
        function has already returned. Only a holder that *is* one of the two
        modules, carrying the *other* one's verb, runs the comparison -- which
        is why the mutants dropping each restriction survived a suite full of
        `copy.copy`. `shutil.remove` is also the mistake a person makes,
        reaching for `os.remove` and typing the module they were already using.
        """
        body = 'return shutil.remove(request.GET["f"])\n'
        assert run(make_project(view(body))) == []

    def test_a_shutil_verb_on_os_is_not_a_sink(self, make_project):
        """The same trade in the other direction: `os.copy` does not exist."""
        body = 'return os.copy(request.GET["f"], BASE)\n'
        assert run(make_project(view(body))) == []

    def test_os_removing_something_still_is(self, make_project):
        """The contrast, so the two above cannot pass by the rule going silent."""
        body = 'return os.remove(request.GET["f"])\n'
        assert len(run(make_project(view(body)))) == 1

    def test_an_arbitrary_object_moving_something_is_not_a_sink(self, make_project):
        body = 'return tarfile.move(request.GET["f"])\n'
        assert run(make_project(view(body))) == []

    def test_shutil_moving_something_still_is(self, make_project):
        body = 'return shutil.move(request.GET["f"], BASE)\n'
        assert len(run(make_project(view(body)))) == 1

    def test_a_clean_binding_is_not_reported(self, make_project):
        """The name resolves, and what it resolves to is a constant."""
        body = 'name = "report.csv"\nreturn open(BASE + name)\n'
        assert run(make_project(view(body))) == []


class TestPercentFormattingWithATuple:
    """A path built with `%` blames the element, not the tuple holding it.

    The traversal walk's tuple branch was reached zero times across all three
    corpora, which is an argument that nobody writes it, not that nobody can.
    Skipping it leaves the finding in place -- the tuple carries the taint --
    and moves the blame onto `(BASE, request.GET['f'])`, which tells the reader
    to look at a line they can already see rather than at the part of it that
    came from the request.
    """

    def blamed(self, project) -> str:
        found = run(project)
        assert len(found) == 1, found
        return found[0].evidence[1].content

    def test_the_tainted_element_of_a_pair(self, make_project):
        body = 'return open("%s/%s" % (BASE, request.GET["f"]))\n'
        assert self.blamed(make_project(view(body))) == "request.GET['f']"

    def test_a_single_element_tuple(self, make_project):
        body = 'return open("%s" % (request.GET["f"],))\n'
        assert self.blamed(make_project(view(body))) == "request.GET['f']"

    def test_a_list_on_the_right(self, make_project):
        body = 'return open("%s" % [request.GET["f"]])\n'
        assert self.blamed(make_project(view(body))) == "request.GET['f']"

    def test_a_tuple_of_constants_is_not_reported(self, make_project):
        """The contrast: walking into the tuple must not invent taint."""
        body = 'return open("%s/%s" % (BASE, "notes.txt"))\n'
        assert run(make_project(view(body))) == []

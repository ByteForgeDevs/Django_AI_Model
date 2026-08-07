"""`DJI-011` -- request data marked as trusted HTML.

The cases that matter are the ones where taint is real and the code is correct:
``format_html`` escaping its arguments, ``escape()`` applied before marking, and
netbox assembling a table cell out of a percent-encoded query parameter.
"""

from __future__ import annotations

import ast
import textwrap

from djaudit.context import ProjectContext
from djaudit.models import Confidence, EvidenceKind, Family, Finding, Severity
from djaudit.registry import all_rules


def run(ctx: ProjectContext) -> list[Finding]:
    rule = next(r for r in all_rules() if r.meta.id == "DJI-011")
    return list(rule().check(ctx))


def view(body: str, extra: str = "") -> dict[str, str]:
    """A project holding one view, which is where a request is in scope."""
    return {
        "manage.py": "import os\n",
        "app/views.py": textwrap.dedent(
            """
            from django.utils.safestring import mark_safe, SafeString
            from django.utils.html import format_html, format_html_join, escape
            from urllib.parse import quote

            TEMPLATE = "<b>{}</b>"
            """
        )
        + textwrap.dedent(extra)
        + textwrap.dedent(
            """

            def show(request):
            """
        )
        + textwrap.indent(textwrap.dedent(body), "    "),
    }


class TestWhenTheRequestIsMarkedSafe:
    def test_a_request_read_marked_safe(self, make_project):
        body = 'return mark_safe(request.GET["bio"])\n'
        assert len(run(make_project(view(body)))) == 1

    def test_a_local_holding_a_request_read(self, make_project):
        body = 'bio = request.GET["bio"]\nreturn mark_safe(bio)\n'
        assert len(run(make_project(view(body)))) == 1

    def test_a_safestring_constructor(self, make_project):
        body = 'return SafeString(request.GET["bio"])\n'
        assert len(run(make_project(view(body)))) == 1

    def test_percent_formatting(self, make_project):
        body = 'return mark_safe("<b>%s</b>" % request.GET["bio"])\n'
        assert len(run(make_project(view(body)))) == 1

    def test_a_percent_format_string_the_request_supplies(self, make_project):
        body = 'return mark_safe(request.GET["tpl"] % 1)\n'
        assert len(run(make_project(view(body)))) == 1

    def test_a_tuple_of_percent_arguments(self, make_project):
        body = 'return mark_safe("<b>%s</b>" % (request.GET["bio"],))\n'
        assert len(run(make_project(view(body)))) == 1

    def test_format_keyword_arguments(self, make_project):
        body = 'return mark_safe("<b>{x}</b>".format(x=request.GET["bio"]))\n'
        assert len(run(make_project(view(body)))) == 1

    def test_str_format_is_looked_inside(self, make_project):
        """Unlike a project's own helper, ``format`` splices verbatim."""
        body = 'return mark_safe("<b>{}</b>".format(request.GET["bio"]))\n'
        assert len(run(make_project(view(body)))) == 1

    def test_a_format_string_the_request_supplies(self, make_project):
        """``format_html`` escapes its arguments and not its format string."""
        body = 'return format_html(request.GET["tpl"], 1)\n'
        assert len(run(make_project(view(body)))) == 1

    def test_format_html_join_format_string(self, make_project):
        body = 'return format_html_join(", ", request.GET["tpl"], rows)\n'
        assert len(run(make_project(view(body)))) == 1


class TestWherePositionCarriesNoMeaning:
    """`DJI-010` reads only the leading part. HTML injects from anywhere."""

    def test_the_request_supplies_the_tail(self, make_project):
        body = 'return mark_safe("<b>" + request.GET["bio"])\n'
        assert len(run(make_project(view(body)))) == 1

    def test_the_request_supplies_the_head(self, make_project):
        body = 'return mark_safe(request.GET["bio"] + "</b>")\n'
        assert len(run(make_project(view(body)))) == 1

    def test_the_request_supplies_the_middle(self, make_project):
        body = "return mark_safe(f'<b>{request.GET[\"bio\"]}</b>')\n"
        assert len(run(make_project(view(body)))) == 1


class TestWhenTheProjectBuildsTheHtml:
    """Every one of these is tainted, and every one is correct code."""

    def test_an_escaped_argument(self, make_project):
        """netbox: ``.format(device=escape(device))`` under ``mark_safe``."""
        body = 'return mark_safe(TEMPLATE.format(escape(request.GET["bio"])))\n'
        assert run(make_project(view(body))) == []

    def test_a_helper_the_project_wrote(self, make_project):
        extra = """
            def decorate(text):
                return "<b>" + text + "</b>"
            """
        body = 'return mark_safe(decorate(request.GET["bio"]))\n'
        assert run(make_project(view(body, extra))) == []

    def test_a_method_the_project_wrote(self, make_project):
        """``format`` is looked inside; another method on an object is not."""
        body = 'return mark_safe(renderer.decorate(request.GET["bio"]))\n'
        assert run(make_project(view(body))) == []

    def test_a_percent_encoded_parameter_reached_twice(self, make_project):
        """netbox's table cell: the name is walked again by a second path.

        The recursion guard has to decline on a repeat visit. Falling through
        to taint instead reported this, which was true about the value and
        wrong about the question.
        """
        body = """
            appendix = f'?return_url={quote(request.GET["return_url"])}'
            button = f'<a href="/x{appendix}">go</a>'
            html = ""
            if button:
                html += button
            html = button + html
            return mark_safe(html)
            """
        assert run(make_project(view(body))) == []


class TestWhatFormatHtmlEscapes:
    def test_the_recommended_call_is_silent(self, make_project):
        """``format_html("<b>{}</b>", tainted)`` is the documented fix."""
        body = 'return format_html("<b>{}</b>", request.GET["bio"])\n'
        assert run(make_project(view(body))) == []

    def test_a_keyword_argument_is_escaped_too(self, make_project):
        body = 'return format_html("<b>{bio}</b>", bio=request.GET["bio"])\n'
        assert run(make_project(view(body))) == []

    def test_a_lone_format_string_raises_rather_than_renders(self, make_project):
        """Django raises TypeError with no args, so this is a crash not a hole.

        Asserted as a contrast: the same format string with an argument beside
        it is reported, so silence here is about the arity and nothing else.
        """
        lone = 'return format_html(request.GET["tpl"])\n'
        armed = 'return format_html(request.GET["tpl"], 1)\n'
        assert run(make_project(view(lone))) == []
        assert len(run(make_project(view(armed)))) == 1

    def test_a_constant_format_string_with_no_arguments(self, make_project):
        assert run(make_project(view('return format_html("<b>hi</b>")\n'))) == []


class TestHowItSpeaks:
    def test_the_finding_names_the_source_and_the_sink(self, make_project):
        body = 'return mark_safe(request.GET["bio"])\n'
        (finding,) = run(make_project(view(body)))
        assert finding.rule_id == "DJI-011"
        assert finding.family is Family.DJI
        assert finding.severity is Severity.HIGH
        assert finding.confidence is Confidence.CERTAIN
        assert "request.GET" in finding.message
        assert finding.properties["sink"] == "mark_safe"
        assert finding.properties["position"] == "argument"
        kinds = {evidence.kind for evidence in finding.evidence}
        assert kinds == {EvidenceKind.AST}
        assert any("mark_safe" in evidence.content for evidence in finding.evidence)

    def test_a_format_string_is_named_as_one(self, make_project):
        body = 'return format_html(request.GET["tpl"], 1)\n'
        (finding,) = run(make_project(view(body)))
        assert finding.properties["position"] == "format string"
        assert "format string" in finding.message


class TestWithNothingToReasonFrom:
    def test_a_project_with_no_python(self, make_project):
        assert run(make_project({"manage.py": "import os\n"})) == []

    def test_a_file_that_never_marks_anything(self, make_project):
        assert run(make_project(view('return "<b>hi</b>"\n'))) == []

    def test_marked_returns_nothing_for_an_unrelated_call(self):
        from djaudit.rules.xss import marked

        node = ast.parse("json.dumps(x)").body[0]
        assert isinstance(node, ast.Expr)
        assert isinstance(node.value, ast.Call)
        assert list(marked(node.value)) == []

    def test_a_sink_with_no_arguments(self, make_project):
        assert run(make_project(view("return mark_safe()\n"))) == []

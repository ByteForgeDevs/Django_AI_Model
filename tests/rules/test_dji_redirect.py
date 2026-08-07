"""`DJI-010` -- a redirect sending the client to a host it chose itself.

The measurements behind this rule are reproduced here as tests. Django blocks
the scheme and not the host; ``redirect()`` will hand an absolute URL straight
through; and all three benchmarks guard their redirects with a project-local
helper rather than by calling Django's validator where the rule can see it.
"""

from __future__ import annotations

import ast
import textwrap

from djaudit.context import ProjectContext
from djaudit.models import Confidence, EvidenceKind, Family, Finding, Severity
from djaudit.registry import all_rules


def run(ctx: ProjectContext) -> list[Finding]:
    rule = next(r for r in all_rules() if r.meta.id == "DJI-010")
    return list(rule().check(ctx))


def view(body: str, extra: str = "") -> dict[str, str]:
    """A project holding one view, which is where a request is in scope."""
    return {
        "manage.py": "import os\n",
        "app/views.py": textwrap.dedent(
            """
            from django.shortcuts import redirect
            from django.http import HttpResponseRedirect
            from django.urls import reverse
            from django.utils.http import url_has_allowed_host_and_scheme

            HOME = "/dashboard/"
            """
        )
        + textwrap.dedent(extra)
        + textwrap.dedent(
            """

            def land(request):
            """
        )
        + textwrap.indent(textwrap.dedent(body), "    "),
    }


class TestWhenTheRequestChoosesTheDestination:
    def test_a_next_parameter_redirected_unchecked(self, make_project):
        found = run(make_project(view('return redirect(request.GET["next"])\n')))
        assert len(found) == 1
        assert found[0].rule_id == "DJI-010"
        assert found[0].family is Family.DJI
        assert found[0].severity is Severity.MEDIUM

    def test_the_get_form_of_the_same_read(self, make_project):
        body = 'return redirect(request.GET.get("next", "/"))\n'
        assert len(run(make_project(view(body)))) == 1

    def test_a_target_bound_to_a_name_first(self, make_project):
        body = textwrap.dedent(
            """
            target = request.POST["next"]
            return redirect(target)
            """
        )
        assert len(run(make_project(view(body)))) == 1

    def test_http_response_redirect_and_its_permanent_form(self, make_project):
        for sink in ("HttpResponseRedirect", "HttpResponsePermanentRedirect"):
            body = f'return {sink}(request.GET["next"])\n'
            found = run(make_project(view(body)))
            assert len(found) == 1, sink
            assert found[0].properties["sink"] == sink

    def test_a_bare_scheme_with_the_host_appended(self, make_project):
        """``"https://" + tainted`` stops inside the authority."""
        body = 'return redirect("https://" + request.GET["h"])\n'
        assert len(run(make_project(view(body)))) == 1

    def test_a_protocol_relative_prefix(self, make_project):
        """``"//" + tainted`` hands the host over as surely as a scheme does."""
        body = 'return redirect("//" + request.GET["h"])\n'
        assert len(run(make_project(view(body)))) == 1

    def test_an_f_string_that_begins_with_the_request(self, make_project):
        body = "return redirect(f\"{request.GET['next']}?ok=1\")\n"
        assert len(run(make_project(view(body)))) == 1


class TestWhenTheProjectChoosesTheDestination:
    def test_reverse_cannot_name_another_host(self, make_project):
        """Measured on pretix: ``redirect(reverse(..., kwargs=self.kwargs))``.

        The call is ``TAINTED``, because a call takes taint from its arguments
        and ``self.kwargs`` is a request source. It is also correct code --
        ``reverse()`` resolves against the urlconf and cannot emit a host.
        """
        body = 'return redirect(reverse("app:edit", kwargs=self.kwargs))\n'
        assert run(make_project(view(body))) == []

    def test_a_projects_own_url_builder(self, make_project):
        """Measured on pretix: ``redirect(self.get_process_url(request, cf))``."""
        body = 'return redirect(self.get_process_url(request, "csv"))\n'
        assert run(make_project(view(body))) == []

    def test_a_signature_check_that_fails_closed(self, make_project):
        """Measured on pretix: an intentional redirector, gated on a signature."""
        body = textwrap.dedent(
            """
            url = signing.Signer(salt="x").unsign(request.GET.get("url", ""))
            return HttpResponseRedirect(url)
            """
        )
        assert run(make_project(view(body))) == []

    def test_a_relative_path_with_the_request_in_it(self, make_project):
        """A leading ``/dashboard/`` settles the host before the payload starts."""
        body = 'return redirect("/dashboard/" + request.GET["tab"])\n'
        assert run(make_project(view(body))) == []

    def test_a_fixed_absolute_prefix(self, make_project):
        body = 'return redirect("https://app.example.com/x/" + request.GET["tab"])\n'
        assert run(make_project(view(body))) == []

    def test_a_query_string_appended_to_a_fixed_path(self, make_project):
        body = "return redirect(f\"/orders/?ref={request.GET['ref']}\")\n"
        assert run(make_project(view(body))) == []

    def test_a_route_name_with_arguments(self, make_project):
        """``redirect("view", pk=x)`` reverses, and its first argument says so.

        The extra arguments are *not* what settles it. An earlier draft looked
        away from any call carrying them, which would also have looked away
        from the redirect in the next test.
        """
        body = 'return redirect("app:order", pk=request.GET["pk"])\n'
        assert run(make_project(view(body))) == []

    def test_a_constant_route_name(self, make_project):
        assert run(make_project(view('return redirect("app:index")\n'))) == []


class TestWhenTheTargetIsChecked:
    def test_djangos_own_validator(self, make_project):
        body = textwrap.dedent(
            """
            target = request.GET["next"]
            if url_has_allowed_host_and_scheme(target, allowed_hosts=None):
                return redirect(target)
            return redirect(HOME)
            """
        )
        assert run(make_project(view(body))) == []

    def test_the_older_name_for_it(self, make_project):
        body = textwrap.dedent(
            """
            target = request.GET["next"]
            if is_safe_url(target):
                return redirect(target)
            return redirect(HOME)
            """
        )
        assert run(make_project(view(body))) == []

    def test_a_wrapper_around_it(self, make_project):
        """Measured on netbox, which wraps the validator in ``safe_for_redirect``."""
        extra = """
            def safe_for_redirect(url):
                return url_has_allowed_host_and_scheme(url, allowed_hosts=None)
            """
        body = textwrap.dedent(
            """
            target = request.GET["next"]
            if safe_for_redirect(target):
                return redirect(target)
            return redirect(HOME)
            """
        )
        assert run(make_project(view(body, extra))) == []

    def test_a_hand_rolled_check_on_the_authority(self, make_project):
        """Measured on healthchecks, which parses the URL and rejects a netloc.

        This is the shape that made resolving validators necessary rather than
        tidy. Across the benchmarks seven redirects are declined by the guard
        and by nothing else, and healthchecks' single guarded redirect is one
        of them -- a rule that knew only Django's function name would report it.
        """
        extra = """
            def _allow_redirect(url):
                parsed = urlparse(url)
                if parsed.netloc:
                    return False
                return True
            """
        body = textwrap.dedent(
            """
            target = request.GET["next"]
            if _allow_redirect(target):
                return redirect(target)
            return redirect(HOME)
            """
        )
        assert run(make_project(view(body, extra))) == []

    def test_the_guard_is_read_through_the_request_it_came_from(self, make_project):
        """The check names the parameter; the redirect names the local."""
        body = textwrap.dedent(
            """
            target = request.GET.get("next")
            if not url_has_allowed_host_and_scheme(request.GET.get("next")):
                target = HOME
            return redirect(target)
            """
        )
        assert run(make_project(view(body))) == []

    def test_a_guard_about_a_different_value(self, make_project):
        """A check on something else must not excuse this redirect."""
        body = textwrap.dedent(
            """
            other = request.GET["back"]
            if url_has_allowed_host_and_scheme(other):
                pass
            return redirect(request.GET["next"])
            """
        )
        assert len(run(make_project(view(body)))) == 1

    def test_a_call_that_is_not_a_validator(self, make_project):
        """``len()`` tests the value without deciding where it points."""
        body = textwrap.dedent(
            """
            target = request.GET["next"]
            if len(target) < 100:
                return redirect(target)
            return redirect(HOME)
            """
        )
        assert len(run(make_project(view(body)))) == 1

    def test_a_permanent_redirect_still_carries_arguments(self, make_project):
        """``permanent=True`` does not make a chosen host any safer."""
        body = 'return redirect(request.GET["next"], permanent=True)\n'
        assert len(run(make_project(view(body)))) == 1

    def test_a_guard_in_a_sibling_scope(self, make_project):
        """A check in another function says nothing about this one."""
        extra = """
            def other(request):
                return url_has_allowed_host_and_scheme(request.GET["next"])
            """
        body = 'return redirect(request.GET["next"])\n'
        assert len(run(make_project(view(body, extra)))) == 1

    def test_a_guard_in_a_nested_scope(self, make_project):
        """A check written inside a closure has not run by the redirect."""
        guarded = """
            def check():
                return url_has_allowed_host_and_scheme(request.GET["next"])

            return redirect(request.GET["next"])
            """
        plain = 'return redirect(request.GET["next"])\n'
        assert len(run(make_project(view(guarded)))) == len(run(make_project(view(plain)))) == 1


class TestWhatValidatesMeans:
    def test_a_forwarder_and_a_parser_both_count(self):
        from djaudit.rules.redirect import validates

        def parse(text: str) -> ast.FunctionDef:
            node = ast.parse(textwrap.dedent(text)).body[0]
            assert isinstance(node, ast.FunctionDef)
            return node

        for text in (
            "def f(u):\n    return url_has_allowed_host_and_scheme(u)\n",
            "def f(u):\n    return is_safe_url(u)\n",
            "def f(u):\n    return not urlparse(u).netloc\n",
            "def f(u):\n    return urlsplit(u).hostname is None\n",
            "def f(u):\n    p = urlparse(u)\n    return p.netloc == ''\n",
        ):
            assert validates(parse(text)) is True, text

        for text in (
            # Parsing alone is not checking, and neither is checking the one
            # thing Django already checks for itself.
            "def f(u):\n    return urlparse(u).scheme == 'https'\n",
            "def f(u):\n    return urlparse(u).path\n",
            "def f(u):\n    return u.netloc\n",
            "def f(u):\n    return len(u) < 100\n",
            "def f(u):\n    return u.startswith('/')\n",
            "def f(u):\n    return u in CHOICES\n",
        ):
            assert validates(parse(text)) is False, text


class TestHowItSpeaks:
    def test_it_names_the_sink_the_target_and_the_source(self, make_project):
        found = run(make_project(view('return redirect(request.GET["next"])\n')))
        assert found[0].properties["sink"] == "redirect"
        assert found[0].properties["target"] == "request.GET['next']"
        assert found[0].properties["source"] == "request.GET"

    def test_it_says_which_host_not_which_path(self, make_project):
        found = run(make_project(view('return redirect(request.GET["next"])\n')))
        assert "host" in found[0].message
        assert found[0].confidence is Confidence.CERTAIN

    def test_it_cites_the_call_as_ast_evidence(self, make_project):
        found = run(make_project(view('return redirect(request.GET["next"])\n')))
        assert found[0].evidence[0].kind is EvidenceKind.AST
        assert "redirect(" in found[0].evidence[0].content


class TestWithNothingToReasonFrom:
    def test_an_empty_project(self, make_project):
        ctx: ProjectContext = make_project({"manage.py": "import os\n"})
        assert run(ctx) == []

    def test_a_redirect_with_no_arguments(self, make_project):
        assert run(make_project(view("return redirect()\n"))) == []

    def test_the_leading_constant_boundary(self):
        """A unit check on which prefixes settle a destination."""
        from djaudit.rules.redirect import _pins

        for text in ("/dashboard/", "https://app.example.com/", "//app.example.com/", "?x="):
            assert _pins(text) is True, text
        for text in ("//", "https://", "http://", ""):
            assert _pins(text) is False, text

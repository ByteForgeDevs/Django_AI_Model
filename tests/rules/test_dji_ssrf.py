"""`DJI-009` -- request data choosing the host of an outbound request.

The two measurements behind this rule are reproduced here as tests. The first
is that receivers cannot be matched by name, because ``self.client.get`` is a
test and ``request.session.get`` is a dictionary. The second is that
``urljoin`` hands over the host while concatenation does not, which is the
opposite of the way the shape is usually linted.
"""

from __future__ import annotations

import ast
import textwrap

from djaudit.context import ProjectContext
from djaudit.models import Confidence, EvidenceKind, Family, Finding, Severity
from djaudit.registry import all_rules


def run(ctx: ProjectContext) -> list[Finding]:
    rule = next(r for r in all_rules() if r.meta.id == "DJI-009")
    return list(rule().check(ctx))


def view(body: str) -> dict[str, str]:
    """A project holding one view, which is where a request is in scope."""
    return {
        "manage.py": "import os\n",
        "app/views.py": textwrap.dedent(
            """
            import requests
            import urllib.request
            from urllib.parse import urljoin

            BASE = "https://api.internal.example.com/v1/"


            def fetch(request):
            """
        )
        + textwrap.indent(textwrap.dedent(body), "    "),
    }


class TestWhenTheRequestChoosesTheHost:
    def test_the_whole_url_is_tainted(self, make_project):
        found = run(make_project(view('requests.get(request.GET["url"])\n')))
        assert len(found) == 1
        assert found[0].rule_id == "DJI-009"
        assert found[0].family is Family.DJI
        assert found[0].severity is Severity.HIGH

    def test_the_url_keyword(self, make_project):
        found = run(make_project(view('requests.get(url=request.GET["url"])\n')))
        assert len(found) == 1

    def test_a_host_interpolated_into_an_f_string(self, make_project):
        body = "requests.get(f\"https://{request.GET['host']}/v1/users\")\n"
        found = run(make_project(view(body)))
        assert len(found) == 1

    def test_a_host_concatenated_onto_a_bare_scheme(self, make_project):
        found = run(make_project(view('requests.get("https://" + request.GET["h"])\n')))
        assert len(found) == 1

    def test_urljoin_hands_over_the_host(self, make_project):
        """Measured: ``urljoin(BASE, "http://evil.com/x")`` resolves to evil.com.

        The authority assertion is what makes this a test of the ``urljoin``
        handling. Without it a mutant that removed the handling entirely still
        passed, because ``urljoin(BASE, tainted)`` is an opaque call with a
        tainted argument and so is itself ``TAINTED`` -- the fallback reported
        the whole call and the count was unchanged. Naming the *reference* is
        the only thing that tells the two apart.
        """
        found = run(make_project(view('requests.get(urljoin(BASE, request.GET["u"]))\n')))
        assert len(found) == 1
        assert found[0].properties["authority"] == "request.GET['u']"

    def test_urljoin_onto_a_reference_the_request_did_not_choose(self, make_project):
        """Only the *reference* hands over the host, so a clean one is silent."""
        body = 'requests.get(urljoin(request.GET["base"], "/health"))\n'
        found = run(make_project(view(body)))
        assert [f.properties["authority"] for f in found] == ["request.GET['base']"]

    def test_urlopen(self, make_project):
        body = 'urllib.request.urlopen(request.GET["url"])\n'
        found = run(make_project(view(body)))
        assert len(found) == 1

    def test_requests_request_takes_the_url_second(self, make_project):
        found = run(make_project(view('requests.request("GET", request.GET["u"])\n')))
        assert len(found) == 1

    def test_a_url_bound_to_a_name_first(self, make_project):
        body = textwrap.dedent(
            """
            target = request.GET["url"]
            requests.get(target)
            """
        )
        assert len(run(make_project(view(body)))) == 1

    def test_post_put_patch_and_delete(self, make_project):
        for verb in ("post", "put", "patch", "delete", "head", "options"):
            body = f'requests.{verb}(request.GET["url"])\n'
            assert len(run(make_project(view(body)))) == 1, verb


class TestWhenTheHostIsFixed:
    def test_a_part_the_rule_cannot_read_ends_the_reasoning(self, make_project):
        """``BASE`` is bound at module level, so the rule cannot read it.

        The conservative reading is the correct one. An unreadable part is far
        more likely to be a full base URL -- which has ended the authority --
        than a bare scheme, so nothing after it can be claimed to choose the
        host. The contrast is
        :meth:`TestWhenTheRequestChoosesTheHost.test_a_host_concatenated_onto_a_bare_scheme`,
        where the preceding part *is* readable and does not end the authority.
        """
        found = run(make_project(view('requests.get(BASE + request.GET["p"])\n')))
        assert found == []

    def test_an_unreadable_part_is_declined_even_though_it_is_tainted(self):
        """The decline is :func:`authority`'s, not the taint model's.

        Every shape in this class is ``TAINTED``, so a rule that asked only
        whether request data reached the URL would report all of them. Probed
        across six declined and four reported shapes; taint was ``TAINTED`` for
        all ten and only the authority walk told them apart.
        """
        from djaudit.dataflow.chains import def_use
        from djaudit.dataflow.scopes import build_scopes
        from djaudit.dataflow.taint import Taint, taint_of
        from djaudit.rules.ssrf import authority

        src = 'def f(request):\n    x = BASE + request.GET["p"]\n'
        tree = ast.parse(src)
        chains = def_use(build_scopes(tree).children[0])
        function = tree.body[0]
        assert isinstance(function, ast.FunctionDef)
        assign = function.body[0]
        assert isinstance(assign, ast.Assign)
        assert taint_of(assign.value, chains) is Taint.TAINTED
        assert authority(assign.value, chains) is None

    def test_concatenation_onto_a_readable_base_that_reaches_the_path(self, make_project):
        """Measured: ``"https://x/v1/" + "http://evil.com"`` keeps the base host."""
        body = 'requests.get("https://api.example.com/v1/" + request.GET["p"])\n'
        assert run(make_project(view(body))) == []

    def test_an_f_string_with_the_value_in_the_path(self, make_project):
        body = "requests.get(f\"https://api.example.com/v1/{request.GET['p']}\")\n"
        assert run(make_project(view(body))) == []

    def test_a_literal_base_with_a_trailing_slash(self, make_project):
        body = 'requests.get("https://api.example.com/" + request.GET["p"])\n'
        assert run(make_project(view(body))) == []

    def test_a_query_string_is_past_the_authority(self, make_project):
        """No slash here, so ``?`` is the only thing ending the authority.

        The original spelling of this test was ``.../x?q=``, which the slash
        already settled; a mutant that dropped ``?`` from the boundary survived
        it. A separator has to be tested where it is the only separator.
        """
        body = 'requests.get("https://api.example.com?q=" + request.GET["q"])\n'
        assert run(make_project(view(body))) == []

    def test_a_fragment_is_past_the_authority(self, make_project):
        body = 'requests.get("https://api.example.com#" + request.GET["f"])\n'
        assert run(make_project(view(body))) == []

    def test_a_constant_reference_that_names_its_own_host(self, make_project):
        """Measured: ``urljoin(EVIL, "http://good/x")`` resolves to ``good``.

        A reference carrying a scheme, or a protocol-relative one, replaces the
        base outright -- so even a base the request chose cannot move the host.
        This is the contrast for
        :meth:`TestWhenTheRequestChoosesTheHost.test_urljoin_onto_a_reference_the_request_did_not_choose`,
        where the reference was relative and the base therefore decided.
        """
        for reference in ('"https://api.example.com/x"', '"//api.example.com/x"'):
            body = f'requests.get(urljoin(request.GET["base"], {reference}))\n'
            assert run(make_project(view(body))) == [], reference

    def test_a_urljoin_reference_the_rule_cannot_read(self, make_project):
        """An unreadable reference might pin the host, so the base is not blamed."""
        body = 'requests.get(urljoin(request.GET["base"], build_path()))\n'
        assert run(make_project(view(body))) == []

    def test_a_clean_reference_joined_onto_a_clean_base(self, make_project):
        body = 'requests.get(urljoin(BASE, "/health") + request.GET["p"])\n'
        assert run(make_project(view(body))) == []

    def test_a_constant_url(self, make_project):
        assert run(make_project(view('requests.get("https://example.com/x")\n'))) == []


class TestWhatANameKeyedRuleWouldHaveMatched:
    def test_the_django_test_client_is_not_an_http_client(self, make_project):
        """4,400 of these across the benchmarks, none of them a fetch."""
        project = {
            "manage.py": "import os\n",
            "app/tests.py": textwrap.dedent(
                """
                class Case:
                    def test_it(self, request):
                        self.client.get(request.GET["path"])
                        self.client.post(request.GET["path"], {})
                """
            ),
        }
        assert run(make_project(project)) == []

    def test_a_session_dictionary_read_is_not_a_fetch(self, make_project):
        """``request.session.get`` is a dictionary read, and server-side.

        Both halves matter. It is not a sink, and the value it returns was put
        there by the application rather than by the client, so even as the URL
        of a real fetch it is not request data. The contrast on the next line is
        what proves the file was analysed at all.
        """
        body = textwrap.dedent(
            """
            requests.get(request.session.get("next", "https://x.test/"))
            requests.get(request.GET["url"])
            """
        )
        found = run(make_project(view(body)))
        assert [f.properties["authority"] for f in found] == ["request.GET['url']"]

    def test_a_bare_client_name_is_not_matched(self, make_project):
        body = 'client.get(request.GET["url"])\n'
        assert run(make_project(view(body))) == []

    def test_a_session_held_on_self_is_not_matched(self, make_project):
        body = 'self.session.get(request.GET["url"])\n'
        assert run(make_project(view(body))) == []

    def test_an_unrelated_get_on_a_dict(self, make_project):
        body = 'requests.get(CHOICES.get(request.GET["k"], "https://x.test/"))\n'
        found = run(make_project(view(body)))
        assert len(found) == 1
        assert found[0].properties["sink"] == "requests.get"


class TestWhatItDeclines:
    def test_an_opaque_parameter_is_not_tainted(self, make_project):
        project = {
            "manage.py": "import os\n",
            "app/client.py": textwrap.dedent(
                """
                import requests


                def pull(request, url):
                    return requests.get(url)
                """
            ),
        }
        assert run(make_project(project)) == []

    def test_a_bare_call_that_is_not_an_opener(self, make_project):
        """Only ``urlopen``/``urlretrieve`` are matched unqualified.

        A project's own helper takes request data all the time. Matching bare
        names generally -- rather than the two that nothing else is called --
        would turn every one of them into a finding.
        """
        body = 'fetch_page(request.GET["url"])\n'
        assert run(make_project(view(body))) == []

    def test_a_call_that_is_not_a_fetcher(self, make_project):
        assert run(make_project(view('requests.utils.quote(request.GET["u"])\n'))) == []

    def test_an_attribute_named_like_the_library_is_not_the_library(self, make_project):
        """``self.requests`` is a stub or a related manager, not the module.

        The same shallow-holder discipline that keeps ``self.client.get`` and
        ``request.session.get`` out also has to keep out an attribute that
        happens to share the library's name.
        """
        body = 'self.requests.get(request.GET["url"])\n'
        assert run(make_project(view(body))) == []

    def test_a_keyword_that_is_not_the_url(self, make_project):
        """The URL keyword is read by name, not by position among keywords."""
        body = "requests.post(json=request.POST, url=BASE)\n"
        assert run(make_project(view(body))) == []

    def test_a_method_with_no_url_after_it(self, make_project):
        """``requests.request`` takes the URL second, so one argument is none.

        Reading ``args[1]`` on the strength of ``args`` being non-empty raises
        rather than declining, which a rule must never do to a real project.
        """
        assert run(make_project(view('requests.request("GET")\n'))) == []

    def test_a_fetch_with_no_arguments(self, make_project):
        assert run(make_project(view("requests.get()\n"))) == []


class TestHowItSpeaks:
    def test_it_names_the_sink_the_url_and_the_authority(self, make_project):
        found = run(make_project(view('requests.get("https://" + request.GET["h"])\n')))
        assert len(found) == 1
        finding = found[0]
        assert finding.properties["sink"] == "requests.get"
        assert "request.GET" in finding.properties["authority"]

    def test_it_says_forgery_not_a_bad_path(self, make_project):
        found = run(make_project(view('requests.get(request.GET["url"])\n')))
        assert "request forgery rather than a bad path" in found[0].message

    def test_it_cites_the_call_as_ast_evidence(self, make_project):
        found = run(make_project(view('requests.get(request.GET["url"])\n')))
        assert found[0].evidence[0].kind is EvidenceKind.AST
        assert "requests.get" in found[0].evidence[0].content

    def test_a_resolved_source_is_certain(self, make_project):
        found = run(make_project(view("requests.get(request.body)\n")))
        assert len(found) == 1
        assert found[0].confidence is Confidence.CERTAIN
        assert found[0].properties["source"] == "request.body"

    def test_a_source_reached_through_a_name_is_still_certain(self, make_project):
        """Unlike `DJI-008`, this rule resolves before it reports.

        :func:`authority` follows a name to its definition and returns what it
        found, so the finding names ``request.GET`` rather than the local it
        arrived in. Every reportable shape therefore has a source, which is why
        the rule has no lower-confidence branch. Probed across nine shapes --
        direct, through one name, through two, wrapped in a call, interpolated,
        through ``urljoin`` -- and a source was resolved for all nine.
        """
        body = textwrap.dedent(
            """
            target = request.GET["url"]
            requests.get(target)
            """
        )
        found = run(make_project(view(body)))
        assert len(found) == 1
        assert found[0].confidence is Confidence.CERTAIN
        assert found[0].properties["source"] == "request.GET"
        assert found[0].properties["url"] == "target"
        assert found[0].properties["authority"] == "request.GET['url']"


class TestWithNothingToReasonFrom:
    def test_an_empty_project(self, make_project):
        ctx: ProjectContext = make_project({"manage.py": "import os\n"})
        assert run(ctx) == []

    def test_the_sink_set_is_matched_by_shape_not_by_the_file_prefilter(self):
        """A unit check on :func:`fetches`, independent of any project."""
        from djaudit.rules.ssrf import fetches

        def parse(text: str) -> ast.Call:
            statement = ast.parse(text).body[0]
            assert isinstance(statement, ast.Expr)
            call = statement.value
            assert isinstance(call, ast.Call)
            return call

        for text in (
            "requests.get(u)",
            "requests.post(u)",
            "httpx.get(u)",
            "httpx.stream(u)",
            "urllib3.request('GET', u)",
            "urllib.request.urlopen(u)",
            "urlopen(u)",
            "requests.get(url=u)",
        ):
            assert len(list(fetches(parse(text)))) == 1, text

        for text in (
            "self.client.get(u)",
            "client.get(u)",
            "session.get(u)",
            "request.session.get(u)",
            "requests.get()",
            "requests.codes(u)",
            "httpx.Client(u)",
            "get(u)",
        ):
            assert list(fetches(parse(text))) == [], text

    def test_the_authority_boundary(self):
        """The measured table, as a unit check on where a constant stops."""
        from djaudit.rules.ssrf import _ends_authority

        for text in ("https://host/", "https://host/v1/", "http://h/x?q=", "//host/x"):
            assert _ends_authority(text), text
        for text in ("https://", "http://", "//", "https://host"):
            assert not _ends_authority(text), text

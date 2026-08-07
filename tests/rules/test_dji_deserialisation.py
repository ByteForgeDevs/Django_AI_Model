"""DJI-007 -- request data reaching a sink that executes what it decodes.

The measurement that shaped this rule concerns YAML. Each PyYAML loader was run
in its own process against ``!!python/object/apply:os.system`` and checked for a
filesystem side effect the return value could not fake: ``Loader``,
``UnsafeLoader`` and ``CLoader`` executed it; ``SafeLoader``, ``FullLoader``,
``CSafeLoader``, ``CFullLoader`` and ``BaseLoader`` did not. And ``yaml.load(x)``
with no loader at all raises ``TypeError`` on PyYAML 6, so the rule everybody
writes -- "flag bare yaml.load" -- reports a crash rather than a vulnerability.

All three ``yaml.load_all`` calls in the benchmark corpus pass
``Loader=yaml.SafeLoader``, one of them on real bulk import data, so a rule that
ignored the keyword would have shipped three false positives and no true ones.
The loader tests therefore have their own class.
"""

from __future__ import annotations

import ast
import textwrap

from djaudit.context import ProjectContext
from djaudit.models import Confidence, EvidenceKind, Family, Finding, Severity
from djaudit.registry import all_rules

SETTINGS = """
SECRET_KEY = "x"
DEBUG = False
ALLOWED_HOSTS = ["example.com"]
INSTALLED_APPS = ["library"]
ROOT_URLCONF = "library.urls"
"""


def findings(make_project, code: str) -> list[Finding]:
    ctx: ProjectContext = make_project(
        {
            "manage.py": "import os\n",
            "library/__init__.py": "",
            "library/settings.py": SETTINGS,
            "library/urls.py": "urlpatterns = []\n",
            "library/views.py": textwrap.dedent(code).lstrip(),
        }
    )
    rule = next(r for r in all_rules() if r.meta.id == "DJI-007")
    return list(rule().check(ctx))


class TestCodeEvaluation:
    def test_eval_of_a_request_parameter(self, make_project):
        found = findings(
            make_project,
            """
            def run(request):
                return eval(request.GET["expr"])
            """,
        )
        assert len(found) == 1
        assert found[0].rule_id == "DJI-007"
        assert found[0].family is Family.DJI
        assert found[0].severity is Severity.CRITICAL
        assert found[0].confidence is Confidence.CERTAIN

    def test_exec_through_a_name(self, make_project):
        found = findings(
            make_project,
            """
            def run(request):
                code = request.POST["code"]
                exec(code)
            """,
        )
        assert len(found) == 1
        assert found[0].confidence is Confidence.FIRM

    def test_a_method_called_eval_is_not_the_builtin(self, make_project):
        """``self.eval(...)`` is somebody's own method, not the evaluator."""
        assert (
            findings(
                make_project,
                """
                class Calculator:
                    def run(self, request):
                        return self.eval(request.GET["expr"])
                """,
            )
            == []
        )

    def test_compile_is_not_a_sink(self, make_project):
        """Documented as a limitation: compile does not run what it builds."""
        assert (
            findings(
                make_project,
                """
                def run(request):
                    return compile(request.GET["expr"], "<s>", "eval")
                """,
            )
            == []
        )


class TestDeserialisers:
    def test_pickle_loads_of_a_request_body(self, make_project):
        found = findings(
            make_project,
            """
            import pickle

            def load(request):
                return pickle.loads(request.body)
            """,
        )
        assert len(found) == 1
        assert found[0].properties["sink"] == "pickle.loads"
        assert found[0].properties["source"] == "request.body"

    def test_marshal_and_dill_and_jsonpickle(self, make_project):
        for module, function in (
            ("marshal", "loads"),
            ("dill", "loads"),
            ("jsonpickle", "decode"),
            ("cPickle", "load"),
        ):
            found = findings(
                make_project,
                f"""
                import {module}

                def load(request):
                    return {module}.{function}(request.body)
                """,
            )
            assert len(found) == 1, (module, function)
            assert found[0].properties["sink"] == f"{module}.{function}"

    def test_dumping_is_not_loading(self, make_project):
        """``pickle.dumps`` is safe on any input -- only reconstruction runs code.

        Matching the module without the function name would report this, and
        serialising request data is an ordinary thing to do.
        """
        assert (
            findings(
                make_project,
                """
                import pickle

                def store(request):
                    pickle.loads(b"")
                    return pickle.dumps(request.body)
                """,
            )
            == []
        )

    def test_json_is_not_a_sink(self, make_project):
        """The whole point of the remediation must not itself be reported."""
        assert (
            findings(
                make_project,
                """
                import json

                def load(request):
                    return json.loads(request.body)
                """,
            )
            == []
        )

    def test_a_load_on_an_unrelated_object(self, make_project):
        """``self.loads`` and ``form.load`` are not the pickle module."""
        assert (
            findings(
                make_project,
                """
                import pickle

                def load(request, form):
                    pickle.loads(b"")
                    return form.load(request.body)
                """,
            )
            == []
        )


class TestTheYamlLoaderArgument:
    def test_an_unsafe_loader_by_keyword(self, make_project):
        for loader in ("Loader", "UnsafeLoader", "CLoader"):
            found = findings(
                make_project,
                f"""
                import yaml

                def load(request):
                    return yaml.load(request.body, Loader=yaml.{loader})
                """,
            )
            assert len(found) == 1, loader
            assert loader in found[0].message

    def test_an_unsafe_loader_positionally(self, make_project):
        """``load(stream, Loader)`` -- the loader is the second positional."""
        found = findings(
            make_project,
            """
            import yaml

            def load(request):
                return yaml.load(request.body, yaml.UnsafeLoader)
            """,
        )
        assert len(found) == 1

    def test_an_unsafe_loader_imported_directly(self, make_project):
        found = findings(
            make_project,
            """
            from yaml import UnsafeLoader
            import yaml

            def load(request):
                return yaml.load(request.body, Loader=UnsafeLoader)
            """,
        )
        assert len(found) == 1

    def test_the_safe_loaders_are_a_real_defence(self, make_project):
        """Measured: none of these executed the payload, so none is a finding.

        This is the netbox shape -- ``yaml.load_all(data, Loader=yaml.SafeLoader)``
        on genuine bulk import data. Reporting it would be the rule's only
        real-world finding and it would be wrong.
        """
        for loader in (
            "SafeLoader",
            "FullLoader",
            "CSafeLoader",
            "CFullLoader",
            "BaseLoader",
        ):
            assert (
                findings(
                    make_project,
                    f"""
                    import yaml

                    def load(request):
                        return yaml.load_all(request.body, Loader=yaml.{loader})
                    """,
                )
                == []
            ), loader

    def test_a_missing_loader_is_not_reported(self, make_project):
        """PyYAML 6 raises TypeError without one; PyYAML 5 defaulted to FullLoader.

        Neither is remote code execution, so the rule everybody writes would be
        reporting a crash. The decline happens inside :func:`sinks`, which the
        prefilter and the rule body share, so it is the loader lookup refusing
        the call rather than the file's text failing a word test.
        """
        assert (
            findings(
                make_project,
                """
                import yaml

                def load(request):
                    return yaml.load(request.body)
                """,
            )
            == []
        )

    def test_a_loader_held_in_a_variable_is_not_reported(self, make_project):
        """Documented as a limitation: the rule cannot show it is one of the three."""
        assert (
            findings(
                make_project,
                """
                import yaml

                def load(request, chosen):
                    return yaml.load(request.body, Loader=chosen)
                """,
            )
            == []
        )

    def test_unsafe_load_needs_no_loader_argument(self, make_project):
        for function in ("unsafe_load", "unsafe_load_all"):
            found = findings(
                make_project,
                f"""
                import yaml

                def load(request):
                    return yaml.{function}(request.body)
                """,
            )
            assert len(found) == 1, function

    def test_safe_load_is_the_remediation(self, make_project):
        assert (
            findings(
                make_project,
                """
                import yaml

                def load(request):
                    return yaml.safe_load(request.body)
                """,
            )
            == []
        )


class TestWhatItDeclines:
    def test_a_constant_payload(self, make_project):
        assert (
            findings(
                make_project,
                """
                import pickle

                def load(request):
                    return pickle.loads(b"\\x80\\x04.")
                """,
            )
            == []
        )

    def test_a_payload_of_unknown_provenance(self, make_project):
        """Never report UNKNOWN. ruff and bandit already flag the bare call."""
        assert (
            findings(
                make_project,
                """
                import pickle

                def load(request, blob):
                    return pickle.loads(blob)
                """,
            )
            == []
        )

    def test_a_sanitised_payload(self, make_project):
        assert (
            findings(
                make_project,
                """
                def run(request):
                    return eval(int(request.GET["n"]))
                """,
            )
            == []
        )

    def test_a_call_with_no_arguments(self, make_project):
        assert (
            findings(
                make_project,
                """
                import pickle

                def load(request):
                    request.GET.get("x")
                    return pickle.loads()
                """,
            )
            == []
        )

    def test_url_captures_without_the_word_request(self, make_project):
        """``self.kwargs`` is the other source word the prefilter admits on."""
        found = findings(
            make_project,
            """
            from django.views.generic import View

            class Run(View):
                def get(self, *args, **kwargs):
                    return eval(self.kwargs["expr"])
            """,
        )
        assert len(found) == 1


class TestHowItSpeaks:
    def test_it_names_the_sink_and_the_payload(self, make_project):
        found = findings(
            make_project,
            """
            import pickle

            def load(request):
                return pickle.loads(request.body)
            """,
        )
        assert "pickle.loads" in found[0].message
        assert "request.body" in found[0].message
        assert "remote code execution" in found[0].message

    def test_it_carries_ast_evidence(self, make_project):
        found = findings(
            make_project,
            """
            def run(request):
                return eval(request.GET["expr"])
            """,
        )
        assert found[0].evidence
        assert found[0].evidence[0].kind is EvidenceKind.AST
        assert "eval" in found[0].evidence[0].content


class TestWithNothingToReasonFrom:
    def test_an_empty_project(self, make_project):
        ctx: ProjectContext = make_project({"manage.py": "import os\n"})
        rule = next(r for r in all_rules() if r.meta.id == "DJI-007")
        assert list(rule().check(ctx)) == []

    def test_the_sink_set_is_matched_by_shape_not_by_the_file_prefilter(self):
        """A unit check on :func:`sinks`, independent of any project.

        The prefilter and the body share one function, so this is the place the
        sink set itself is pinned down.
        """
        from djaudit.rules.deserialisation import sinks

        def parse(text: str) -> ast.expr:
            statement = ast.parse(text).body[0]
            assert isinstance(statement, ast.Expr)
            return statement.value

        for text in (
            "eval(x)",
            "exec(x)",
            "pickle.loads(x)",
            "yaml.unsafe_load(x)",
            "yaml.load(x, Loader=yaml.Loader)",
        ):
            assert len(list(sinks(parse(text)))) == 1, text

        for text in (
            "compile(x)",
            "pickle.dumps(x)",
            "yaml.dump(x)",
            "json.loads(x)",
            "yaml.safe_load(x)",
            "yaml.load(x)",
            "yaml.load(x, Loader=yaml.SafeLoader)",
            "self.eval(x)",
            "eval()",
            "eval(*args)",
        ):
            assert list(sinks(parse(text))) == [], text

    def test_the_file_prefilter_reads_whole_identifiers(self):
        """A unit check on :func:`names_a_sink`, the gate in front of everything.

        It has to admit every word the matcher can act on and reject the longer
        identifiers those words merely sit inside, or the rule is either unsound
        or pays for an AST walk of the whole project.
        """
        from djaudit.rules.deserialisation import WORDS, names_a_sink

        for word in WORDS:
            assert names_a_sink(f"{word}(payload)"), word
            assert names_a_sink(f"x = {word}\n"), word
            assert names_a_sink(word), word

        # A near miss earlier in the file must not stop the scan short of a
        # real token later in it.
        assert names_a_sink("cursor.execute(sql)\nexec(payload)\n")
        assert names_a_sink("self.evaluate(form)\neval(payload)\n")

        for text in (
            "cursor.execute(sql)",
            "self.evaluate(form)",
            "marshalling = True\n",
            "download(payload)\n",
            "yamllint.run()",
            "unpickler.load(fh)",
            "",
            "import os\n",
        ):
            assert not names_a_sink(text), text

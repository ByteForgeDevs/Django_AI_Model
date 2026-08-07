"""`DJI-008` -- request data reaching a shell.

The measured table in the rule's docstring is reproduced here as tests, because
the three surprising rows are the whole reason the rule is not a grep for
``shell=True``: a string with no shell raises rather than injects, a list with
``shell=True`` only executes its first element, and ``["sh", "-c", x]`` injects
with no ``shell`` keyword anywhere.
"""

from __future__ import annotations

import ast
import textwrap

from djaudit.context import ProjectContext
from djaudit.models import Confidence, EvidenceKind, Family, Finding, Severity
from djaudit.registry import all_rules


def run(ctx: ProjectContext) -> list[Finding]:
    rule = next(r for r in all_rules() if r.meta.id == "DJI-008")
    return list(rule().check(ctx))


def view(body: str) -> dict[str, str]:
    """A project holding one view, which is where a request is in scope."""
    return {
        "manage.py": "import os\n",
        "app/views.py": textwrap.dedent(
            """
            import os
            import shlex
            import subprocess


            def report(request):
            """
        )
        + textwrap.indent(textwrap.dedent(body), "    "),
    }


class TestWhatReachesAShell:
    def test_os_system(self, make_project):
        found = run(make_project(view('os.system("wc -l " + request.GET["f"])\n')))
        assert len(found) == 1
        assert found[0].rule_id == "DJI-008"
        assert found[0].family is Family.DJI
        assert found[0].severity is Severity.CRITICAL

    def test_os_popen(self, make_project):
        found = run(make_project(view('os.popen("wc -l " + request.GET["f"])\n')))
        assert len(found) == 1

    def test_subprocess_getoutput_is_always_a_shell(self, make_project):
        found = run(make_project(view('subprocess.getoutput("wc " + request.GET["f"])\n')))
        assert len(found) == 1

    def test_subprocess_getstatusoutput_is_always_a_shell(self, make_project):
        found = run(make_project(view('subprocess.getstatusoutput(request.GET["c"])\n')))
        assert len(found) == 1

    def test_run_with_shell_true(self, make_project):
        found = run(make_project(view('subprocess.run("wc " + request.GET["f"], shell=True)\n')))
        assert len(found) == 1

    def test_popen_with_shell_true(self, make_project):
        found = run(make_project(view('subprocess.Popen(request.GET["c"], shell=True)\n')))
        assert len(found) == 1

    def test_check_output_with_shell_true(self, make_project):
        found = run(make_project(view('subprocess.check_output(request.GET["c"], shell=True)\n')))
        assert len(found) == 1

    def test_an_f_string_command(self, make_project):
        found = run(make_project(view("subprocess.run(f\"wc {request.GET['f']}\", shell=True)\n")))
        assert len(found) == 1


class TestTheShapesThatMeasurementChanged:
    def test_a_string_without_a_shell_is_a_crash_not_an_injection(self, make_project):
        """Measured: FileNotFoundError. The whole string is one program name."""
        found = run(make_project(view('subprocess.run("wc " + request.GET["f"])\n')))
        assert found == []

    def test_a_list_argument_is_not_a_command_line(self, make_project):
        found = run(make_project(view('subprocess.run(["wc", "-l", request.GET["f"]])\n')))
        assert found == []

    def test_shell_true_reads_only_the_first_element_of_a_list(self, make_project):
        """Measured: ["echo", payload] with shell=True did not run the payload."""
        found = run(make_project(view('subprocess.run(["wc", request.GET["f"]], shell=True)\n')))
        assert found == []

    def test_shell_true_does_report_a_tainted_first_element(self, make_project):
        found = run(make_project(view('subprocess.run([request.GET["c"], "x"], shell=True)\n')))
        assert len(found) == 1
        assert "first element" in found[0].message

    def test_a_shell_program_injects_with_no_shell_keyword(self, make_project):
        found = run(make_project(view('subprocess.run(["sh", "-c", "wc " + request.GET["f"]])\n')))
        assert len(found) == 1
        assert "-c" in found[0].message

    def test_bash_with_an_absolute_path_is_still_a_shell(self, make_project):
        found = run(make_project(view('subprocess.run(["/bin/bash", "-c", request.GET["c"]])\n')))
        assert len(found) == 1

    def test_a_non_shell_program_is_not_one(self, make_project):
        found = run(make_project(view('subprocess.run(["grep", "-c", request.GET["c"]])\n')))
        assert found == []

    def test_an_argument_after_the_command_is_not_executed(self, make_project):
        """Everything past the command string becomes the shell's own $0, $1..."""
        body = 'subprocess.run(["sh", "-c", "wc -l", request.GET["f"]])\n'
        assert run(make_project(view(body))) == []


class TestTheQuotingGuard:
    def test_shlex_quote_at_the_call(self, make_project):
        body = 'subprocess.run("wc " + shlex.quote(request.GET["f"]), shell=True)\n'
        assert run(make_project(view(body))) == []

    def test_shlex_quote_in_an_f_string(self, make_project):
        body = "subprocess.run(f\"wc {shlex.quote(request.GET['f'])}\", shell=True)\n"
        assert run(make_project(view(body))) == []

    def test_shlex_join(self, make_project):
        body = 'subprocess.run(shlex.join(["wc", request.GET["f"]]), shell=True)\n'
        assert run(make_project(view(body))) == []

    def test_quoting_written_where_the_name_was_built(self, make_project):
        """The payload at the call is a bare name; the quoting is a statement up."""
        body = textwrap.dedent(
            """
            cmd = "wc -l " + shlex.quote(request.GET["f"])
            subprocess.run(cmd, shell=True)
            """
        )
        assert run(make_project(view(body))) == []

    def test_an_unquoted_name_built_the_same_way_is_reported(self, make_project):
        """The contrast the previous test needs: same shape, no shlex."""
        body = textwrap.dedent(
            """
            cmd = "wc -l " + request.GET["f"]
            subprocess.run(cmd, shell=True)
            """
        )
        found = run(make_project(view(body)))
        assert len(found) == 1

    def test_quoting_an_unrelated_value_does_not_excuse_the_command(self, make_project):
        body = textwrap.dedent(
            """
            safe = shlex.quote(request.GET["name"])
            subprocess.run("wc " + request.GET["f"], shell=True)
            """
        )
        found = run(make_project(view(body)))
        assert len(found) == 1

    def test_quoting_one_part_does_not_excuse_another_in_the_same_command(self, make_project):
        """The dangerous near-miss: a quoter is present, on the wrong value.

        Only tainted parts are asked whether they were quoted, so a
        ``shlex.quote`` around something safe cannot vouch for the request value
        sitting next to it in the very same expression.
        """
        body = textwrap.dedent(
            """
            prefix = "wc -l"
            subprocess.run(shlex.quote(prefix) + request.GET["f"], shell=True)
            """
        )
        found = run(make_project(view(body)))
        assert len(found) == 1

    def test_a_quote_from_another_module_is_not_shlex(self, make_project):
        """Quoting was measured for shlex. Any other quote() is an unknown."""
        body = 'subprocess.run("wc " + html.quote(request.GET["f"]), shell=True)\n'
        found = run(make_project(view(body)))
        assert len(found) == 1


class TestWhatItDeclines:
    def test_a_constant_command(self, make_project):
        assert run(make_project(view('os.system("wc -l /etc/hosts")\n'))) == []

    def test_a_shell_keyword_that_is_a_variable(self, make_project):
        body = textwrap.dedent(
            """
            flag = True
            subprocess.run(request.GET["c"], shell=flag)
            """
        )
        assert run(make_project(view(body))) == []

    def test_shell_false_written_explicitly(self, make_project):
        assert run(make_project(view('subprocess.run(request.GET["c"], shell=False)\n'))) == []

    def test_an_opaque_parameter_is_not_tainted(self, make_project):
        project = {
            "manage.py": "import os\n",
            "app/tasks.py": textwrap.dedent(
                """
                import os


                def sweep(request, target):
                    os.system("rm -rf " + target)
                """
            ),
        }
        assert run(make_project(project)) == []

    def test_a_call_that_is_not_a_runner(self, make_project):
        assert run(make_project(view('subprocess.list2cmdline(request.GET["c"])\n'))) == []

    def test_an_aliased_module_is_outside_the_matcher(self, make_project):
        """_module reads the immediate holder only, which is what makes the
        text prefilter sound."""
        project = {
            "manage.py": "import os\n",
            "app/views.py": textwrap.dedent(
                """
                import os as operating


                def report(request):
                    operating.system(request.GET["c"])
                """
            ),
        }
        assert run(make_project(project)) == []

    def test_a_dotted_holder_is_not_the_module(self, make_project):
        """``vendor.os.system`` is somebody else's ``system``, not the stdlib's.

        Only the immediate holder is read, so a longer chain that merely ends in
        a module's name is not treated as that module.
        """
        assert run(make_project(view('vendor.os.system(request.GET["c"])\n'))) == []
        body = 'vendor.subprocess.run(request.GET["c"], shell=True)\n'
        assert run(make_project(view(body))) == []


class TestHowItSpeaks:
    def test_it_names_the_sink_the_payload_and_the_source(self, make_project):
        found = run(make_project(view('os.system("wc " + request.GET["f"])\n')))
        assert len(found) == 1
        finding = found[0]
        assert "os.system" in finding.message
        assert finding.properties["sink"] == "os.system"
        assert "request.GET" in finding.properties["payload"]

    def test_it_says_this_is_injection_not_a_bad_argument(self, make_project):
        found = run(make_project(view('os.system("wc " + request.GET["f"])\n')))
        assert "command injection rather than a bad argument" in found[0].message

    def test_it_cites_the_call_as_ast_evidence(self, make_project):
        found = run(make_project(view('os.system("wc " + request.GET["f"])\n')))
        assert found[0].evidence[0].kind is EvidenceKind.AST
        assert "os.system" in found[0].evidence[0].content

    def test_a_resolved_source_is_certain(self, make_project):
        found = run(make_project(view("os.system(request.body)\n")))
        assert len(found) == 1
        assert found[0].confidence is Confidence.CERTAIN
        assert found[0].properties["source"] == "request.body"

    def test_an_unresolved_source_is_only_firm(self, make_project):
        """The contrast the previous test needs.

        Taint follows a name to its definition, but ``_source_of`` reads only
        the expression written at the call. When that expression is a local
        name, the finding is still made and still correct, one confidence step
        down, and says ``unknown`` rather than naming a source it did not
        resolve.
        """
        body = textwrap.dedent(
            """
            target = request.GET["f"]
            os.system("wc " + target)
            """
        )
        found = run(make_project(view(body)))
        assert len(found) == 1
        assert found[0].confidence is Confidence.FIRM
        assert found[0].properties["source"] == "unknown"


class TestWithNothingToReasonFrom:
    def test_an_empty_project(self, make_project):
        ctx: ProjectContext = make_project({"manage.py": "import os\n"})
        assert run(ctx) == []

    def test_the_sink_set_is_matched_by_shape_not_by_the_file_prefilter(self):
        """A unit check on :func:`commands`, independent of any project.

        The prefilter and the body share this function, so this is the place
        the sink set itself is pinned down.
        """
        from djaudit.rules.shell import commands

        def parse(text: str) -> ast.Call:
            statement = ast.parse(text).body[0]
            assert isinstance(statement, ast.Expr)
            call = statement.value
            assert isinstance(call, ast.Call)
            return call

        for text in (
            "os.system(x)",
            "os.popen(x)",
            "subprocess.getoutput(x)",
            "subprocess.getstatusoutput(x)",
            "subprocess.run(x, shell=True)",
            "subprocess.call(x, shell=True)",
            "subprocess.check_call(x, shell=True)",
            "subprocess.check_output(x, shell=True)",
            "subprocess.Popen(x, shell=True)",
            "subprocess.run(['sh', '-c', x])",
            "subprocess.run(['zsh', '-c', x])",
        ):
            assert len(list(commands(parse(text)))) == 1, text

        for text in (
            "subprocess.run(x)",
            "subprocess.run([x])",
            "subprocess.run(['wc', x])",
            "subprocess.run(['sh', x])",
            "subprocess.run(['sh', '-c'])",
            "os.system()",
            "os.getenv(x)",
            "subprocess.list2cmdline(x)",
            "shlex.quote(x)",
            "system(x)",
        ):
            assert list(commands(parse(text))) == [], text

    def test_shell_true_with_a_list_selects_element_zero(self):
        """The row the sentinel changed, pinned as a unit check.

        ``["wc", x]`` with ``shell=True`` is still a sink *shape* -- there is a
        command line, it is just element 0. What declines it is taint, because
        element 0 is a constant. Asserting that here rather than an empty result
        keeps the two questions apart.
        """
        from djaudit.rules.shell import commands

        def payload_of(text: str) -> str:
            statement = ast.parse(text).body[0]
            assert isinstance(statement, ast.Expr)
            call = statement.value
            assert isinstance(call, ast.Call)
            found = list(commands(call))
            assert len(found) == 1, text
            return ast.unparse(found[0].payload)

        assert payload_of("subprocess.run(['a', 'b'], shell=True)") == "'a'"
        assert payload_of("subprocess.run(['wc', x], shell=True)") == "'wc'"
        assert payload_of("subprocess.run(['sh', '-c', x])") == "x"
        assert payload_of("subprocess.run(x, shell=True)") == "x"

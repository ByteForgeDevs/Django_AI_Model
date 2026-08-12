"""The MCP server, tested at the wire rather than through its helpers.

Every test here drives `serve` with the bytes a client would send and reads the
bytes it sends back, because the failure this module is exposed to is not a
wrong return value -- it is a well-formed object on a corrupted stream, or an
error delivered in a shape the model cannot read. Neither is visible from
inside a function.

The protocol shapes asserted here were taken from the published schema at
`schema/2025-06-18/schema.ts`, not from a summary of it: a search for the same
information returned `mcp_version`, `client_id` and `tool_id`, none of which
exist. A server built on those names would have passed every test its author
wrote and failed against every real client.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from djaudit.mcp import PROTOCOL_VERSION, TOOLS
from djaudit.mcp.server import INSTRUCTIONS, serve

HELLO: dict[str, Any] = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    },
}


def converse(*messages: dict[str, Any]) -> list[dict[str, Any]]:
    """Send these messages down the wire and return whatever comes back."""
    stdin = io.StringIO("\n".join(json.dumps(m) for m in messages) + "\n")
    stdout = io.StringIO()
    serve(stdin, stdout)
    return [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]


def call(name: str, arguments: dict[str, Any], identifier: int = 2) -> dict[str, Any]:
    request = {
        "jsonrpc": "2.0",
        "id": identifier,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    result: dict[str, Any] = converse(HELLO, request)[-1]["result"]
    return result


SECURE = """\
import os

SECRET_KEY = os.environ['DJANGO_SECRET_KEY']
DEBUG = False
ALLOWED_HOSTS = ['example.com']
INSTALLED_APPS = []
SESSION_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
CSRF_COOKIE_SECURE = True
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = 'Lax'
SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = 'same-origin'
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
X_FRAME_OPTIONS = 'DENY'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
"""

MANAGE = "import os\nos.environ.setdefault('DJANGO_SETTINGS_MODULE', 'conf.settings')\n"


def scaffold(root: Path, settings: str) -> Path:
    """The minimum shape discovery recognises: manage.py plus a settings package."""
    (root / "conf").mkdir(parents=True)
    (root / "manage.py").write_text(MANAGE)
    (root / "conf" / "__init__.py").write_text("")
    (root / "conf" / "settings.py").write_text(settings)
    return root


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A project whose six findings span three severities and two families.

    The exact contents were measured, not assumed: an earlier version of this
    fixture put settings in a top-level ``conf.py``, which discovery does not
    treat as a settings module, so it produced one finding instead of six and
    the severity-ordering test had nothing to order.
    """
    root = scaffold(
        tmp_path / "proj",
        "SECRET_KEY = 'django-insecure-abcdefghijklmnop'\n"
        "DEBUG = True\n"
        "ALLOWED_HOSTS = ['*']\n"
        "INSTALLED_APPS = ['app']\n",
    )
    (root / "app").mkdir()
    (root / "app" / "__init__.py").write_text("")
    (root / "app" / "views.py").write_text(
        "from django.db import connection\n\n\n"
        "def search(request):\n"
        "    with connection.cursor() as c:\n"
        "        c.execute('SELECT * FROM t WHERE a = ' + request.GET['a'])\n"
    )
    return root


class TestTheHandshake:
    def test_it_answers_initialize_with_a_version_and_a_name(self):
        result = converse(HELLO)[0]["result"]
        assert result["serverInfo"]["name"] == "djaudit"
        assert result["protocolVersion"] == PROTOCOL_VERSION

    def test_it_declares_the_tools_capability(self):
        """A client that is not told about tools will never call one."""
        assert "tools" in converse(HELLO)[0]["result"]["capabilities"]

    def test_it_answers_an_older_client_in_that_client_s_version(self):
        """Downgrading is the compatible direction; the tools surface is unchanged."""
        older = {**HELLO, "params": {**HELLO["params"], "protocolVersion": "2024-11-05"}}
        assert converse(older)[0]["result"]["protocolVersion"] == "2024-11-05"

    def test_it_answers_an_unknown_version_in_its_own(self):
        """A control: echoing anything at all would claim a dialect we cannot speak."""
        alien = {**HELLO, "params": {**HELLO["params"], "protocolVersion": "1999-01-01"}}
        assert converse(alien)[0]["result"]["protocolVersion"] == PROTOCOL_VERSION

    def test_the_instructions_tell_the_model_to_audit_after_writing(self):
        """This field reaches the system prompt, and is why the loop happens at all."""
        instructions = converse(HELLO)[0]["result"]["instructions"]
        assert "audit_django_project" in instructions
        assert instructions == INSTRUCTIONS

    def test_a_notification_is_not_answered(self):
        """A response to a message with no id is a protocol violation."""
        note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        assert len(converse(HELLO, note)) == 1


class TestTheToolList:
    def test_every_tool_is_offered(self):
        tools = converse(HELLO, {"jsonrpc": "2.0", "id": 9, "method": "tools/list"})[-1]
        assert {t["name"] for t in tools["result"]["tools"]} == {t["name"] for t in TOOLS}

    @pytest.mark.parametrize("tool", TOOLS, ids=lambda t: str(t["name"]))
    def test_each_tool_declares_an_object_schema(self, tool: dict[str, Any]):
        """A client validates arguments against this before it will send them."""
        assert tool["inputSchema"]["type"] == "object"
        assert tool["description"]

    def test_the_audit_tool_requires_a_path(self):
        audit = next(t for t in TOOLS if t["name"] == "audit_django_project")
        assert audit["inputSchema"]["required"] == ["path"]


class TestAuditing:
    def test_it_reports_the_defects(self, project: Path):
        result = call("audit_django_project", {"path": str(project)})
        assert result["isError"] is False
        found = {f["rule_id"] for f in result["structuredContent"]["findings"]}
        assert {"DJS-001", "DJS-003", "DJI-001"} <= found

    def test_a_clean_project_says_so_rather_than_going_quiet(self, tmp_path: Path):
        """Silence and success are the same bytes unless one of them says which."""
        root = scaffold(tmp_path / "clean", SECURE)
        result = call("audit_django_project", {"path": str(root)})
        assert "No findings" in result["content"][0]["text"]

    def test_the_text_carries_what_a_model_needs_to_repair(self, project: Path):
        """Rule, place and fix. A count would be a notification, not a work item."""
        text = call("audit_django_project", {"path": str(project)})["content"][0]["text"]
        assert "DJI-001" in text
        assert "app/views.py" in text
        assert "FIX:" in text

    def test_findings_are_ordered_most_severe_first(self, project: Path):
        text = call("audit_django_project", {"path": str(project)})["content"][0]["text"]
        assert text.index("critical") < text.index("high")

    def test_a_family_filter_narrows_the_result(self, project: Path):
        result = call("audit_django_project", {"path": str(project), "families": ["DJI"]})
        families = {f["family"] for f in result["structuredContent"]["findings"]}
        assert families == {"DJI"}

    def test_a_severity_floor_narrows_the_result(self, project: Path):
        result = call("audit_django_project", {"path": str(project), "min_severity": "critical"})
        severities = {f["severity"] for f in result["structuredContent"]["findings"]}
        assert severities <= {"critical"}

    def test_an_incomplete_analysis_is_announced_before_the_findings(self, tmp_path: Path):
        """A short audit that reads as a clean one is the failure djaudit exists to avoid."""
        root = tmp_path / "opaque"
        root.mkdir()
        (root / "manage.py").write_text(MANAGE)
        text = call("audit_django_project", {"path": str(root)})["content"][0]["text"]
        assert text.startswith("ANALYSIS INCOMPLETE")


class TestFailuresTheModelMustBeAbleToRead:
    """The specification puts tool failures in the result, not in a JSON-RPC error.

    A protocol error is invisible to the model: the client raises, and the model
    never learns that the path it guessed was wrong. Every one of these has to
    come back as a readable sentence with ``isError`` set.
    """

    def test_a_missing_directory_is_a_tool_error(self):
        result = call("audit_django_project", {"path": "/no/such/place"})
        assert result["isError"] is True
        assert "Not a directory" in result["content"][0]["text"]

    def test_a_missing_path_is_a_tool_error(self):
        result = call("audit_django_project", {})
        assert result["isError"] is True
        assert "'path' is required" in result["content"][0]["text"]

    def test_an_unknown_tool_names_the_ones_that_exist(self):
        result = call("wrong_name", {})
        assert result["isError"] is True
        assert "audit_django_project" in result["content"][0]["text"]

    def test_an_unknown_family_names_the_ones_that_exist(self, project: Path):
        result = call("audit_django_project", {"path": str(project), "families": ["DJZ"]})
        assert result["isError"] is True
        assert "DJS" in result["content"][0]["text"]

    def test_an_unknown_severity_is_a_tool_error(self, project: Path):
        result = call("audit_django_project", {"path": str(project), "min_severity": "urgent"})
        assert result["isError"] is True

    def test_a_successful_call_is_not_flagged_as_an_error(self, project: Path):
        """The control: every assertion above is worthless if isError is always true."""
        assert call("audit_django_project", {"path": str(project)})["isError"] is False


class TestProtocolErrorsThatAreNotToolErrors:
    def test_an_unknown_method_is_a_protocol_error(self):
        """Unlike a bad argument, this is the client's mistake, not the model's."""
        unknown = {"jsonrpc": "2.0", "id": 5, "method": "resources/list"}
        assert converse(HELLO, unknown)[-1]["error"]["code"] == -32601

    def test_a_malformed_line_is_reported_and_the_session_survives(self):
        """A client that sends one bad line must not lose the connection."""
        stdin = io.StringIO("not json\n" + json.dumps(HELLO) + "\n")
        stdout = io.StringIO()
        serve(stdin, stdout)
        replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
        assert replies[0]["error"]["code"] == -32700
        assert replies[1]["result"]["serverInfo"]["name"] == "djaudit"

    def test_ping_is_answered(self):
        ping = {"jsonrpc": "2.0", "id": 7, "method": "ping"}
        assert converse(HELLO, ping)[-1]["result"] == {}


class TestTheStreamStaysParseable:
    """stdout is the wire. Anything else written to it corrupts the session."""

    def test_every_reply_is_exactly_one_line(self, project: Path):
        stdin = io.StringIO(
            json.dumps(HELLO)
            + "\n"
            + json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "audit_django_project",
                        "arguments": {"path": str(project)},
                    },
                }
            )
            + "\n"
        )
        stdout = io.StringIO()
        serve(stdin, stdout)
        lines = stdout.getvalue().splitlines()
        assert len(lines) == 2
        for line in lines:
            json.loads(line)

    def test_a_finding_with_a_newline_in_it_does_not_break_the_stream(self, project: Path):
        """Remediations are multi-line prose, which is exactly the hazard here."""
        text = call("audit_django_project", {"path": str(project)})["content"][0]["text"]
        assert "\n" in text


class TestExplaining:
    def test_it_explains_a_finding_by_fingerprint(self, project: Path):
        audit = call("audit_django_project", {"path": str(project)})
        fingerprint = audit["structuredContent"]["findings"][0]["fingerprint"]
        result = call("explain_django_finding", {"path": str(project), "fingerprint": fingerprint})
        assert result["isError"] is False
        assert "What is wrong" in result["content"][0]["text"]

    def test_an_unknown_fingerprint_is_a_readable_error(self, project: Path):
        result = call("explain_django_finding", {"path": str(project), "fingerprint": "0" * 16})
        assert result["isError"] is True

    def test_a_missing_fingerprint_is_a_readable_error(self, project: Path):
        result = call("explain_django_finding", {"path": str(project)})
        assert result["isError"] is True
        assert "fingerprint" in result["content"][0]["text"]


class TestTheCatalogue:
    def test_it_lists_every_rule(self):
        result = call("list_django_rules", {})
        assert len(result["structuredContent"]["rules"]) == 87

    def test_one_family_can_be_asked_for(self):
        result = call("list_django_rules", {"family": "DJP"})
        assert {r["family"] for r in result["structuredContent"]["rules"]} == {"DJP"}

    def test_an_unknown_family_is_a_readable_error(self):
        assert call("list_django_rules", {"family": "NOPE"})["isError"] is True

"""The provider that opens a socket, tested without opening one.

``poster`` and ``sleeper`` are injected for one reason: a retry policy that is
only exercised against a live service is a retry policy nobody has run. Every
branch here -- timeout, 429, 400, truncation, a reply in the wrong shape, a
model inventing a field -- is a branch that will happen in production and would
otherwise be reached for the first time there.

The class that matters most is ``TestTheKeyDoesNotEscape``. djaudit reports
`DJS-002` for hardcoded secrets, and a tool that printed one into a CI log
while doing so would deserve to be uninstalled.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from djaudit.llm.config import Credential
from djaudit.llm.http import (
    DEFAULT_BASE,
    MAX_ATTEMPTS,
    Endpoint,
    HTTPProvider,
    TransportError,
    build,
)
from djaudit.llm.provider import Answer, Declined, Field, FieldKind, Prompt, ResponseSchema

SCHEMA = ResponseSchema(
    fields=(
        Field("verdict", FieldKind.STRING, "the call", choices=("yes", "no")),
        Field("why", FieldKind.STRING, "the reason"),
    )
)

PROMPT = Prompt(version="1", system="be terse", user="is this a defect?", schema=SCHEMA)

GOOD = {"verdict": "yes", "why": "it concatenates user input into SQL"}


class Recorder:
    """A stand-in for the network that replays a script and remembers the calls."""

    def __init__(self, *responses: tuple[int, str] | Exception) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        timeout: float,
    ) -> tuple[int, str]:
        self.calls.append({"url": url, "payload": payload, "headers": headers, "timeout": timeout})
        outcome = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class Clock:
    """Records the sleeps instead of taking them."""

    def __init__(self) -> None:
        self.slept: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)


def openai_body(payload: dict[str, Any], **extra: Any) -> str:
    body = {
        "choices": [{"message": {"content": json.dumps(payload)}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }
    body.update(extra)
    return json.dumps(body)


def anthropic_body(payload: dict[str, Any], **extra: Any) -> str:
    body = {
        "content": [{"type": "tool_use", "name": "respond", "input": payload}],
        "usage": {"input_tokens": 100, "output_tokens": 20},
        "stop_reason": "tool_use",
    }
    body.update(extra)
    return json.dumps(body)


def provider(
    *responses: tuple[int, str] | Exception,
    vendor: str = "openai",
    env: str = "TEST_KEY",
) -> tuple[HTTPProvider, Recorder, Clock]:
    poster = Recorder(*responses)
    clock = Clock()
    built = HTTPProvider(
        endpoint=Endpoint(vendor=vendor, base_url=DEFAULT_BASE[vendor]),
        model="m",
        credential=Credential(env),
        poster=poster,
        sleeper=clock,
    )
    return built, poster, clock


@pytest.fixture(autouse=True)
def key(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("TEST_KEY", "sk-secret-value-do-not-log-me")
    return "sk-secret-value-do-not-log-me"


class TestTheHappyPath:
    def test_an_openai_reply_becomes_a_validated_answer(self):
        asked, _, _ = provider((200, openai_body(GOOD)))
        reply = asked.ask(PROMPT)
        assert isinstance(reply, Answer)
        assert reply.content == GOOD

    def test_an_anthropic_reply_becomes_a_validated_answer(self):
        asked, _, _ = provider((200, anthropic_body(GOOD)), vendor="anthropic")
        reply = asked.ask(PROMPT)
        assert isinstance(reply, Answer)
        assert reply.content == GOOD

    def test_the_cost_is_reported_so_the_budget_can_be_enforced(self):
        """A call whose cost is unknown is charged an estimate; a known one is not."""
        asked, _, _ = provider((200, openai_body(GOOD)))
        reply = asked.ask(PROMPT)
        assert isinstance(reply, Answer)
        assert reply.usage.input_tokens == 100
        assert reply.usage.output_tokens == 20

    def test_anthropic_usage_is_read_from_its_own_field_names(self):
        asked, _, _ = provider((200, anthropic_body(GOOD)), vendor="anthropic")
        reply = asked.ask(PROMPT)
        assert isinstance(reply, Answer)
        assert reply.usage.total == 120

    def test_one_success_is_one_call(self):
        asked, poster, _ = provider((200, openai_body(GOOD)))
        asked.ask(PROMPT)
        assert len(poster.calls) == 1


class TestWhatIsSentOnTheWire:
    def test_the_declared_schema_is_what_the_model_is_shown(self):
        """If the request carried a different schema, validation would prove nothing."""
        asked, poster, _ = provider((200, openai_body(GOOD)))
        asked.ask(PROMPT)
        sent = poster.calls[0]["payload"]["response_format"]["json_schema"]["schema"]
        assert sent == SCHEMA.as_json_schema()

    def test_anthropic_is_forced_to_use_the_tool(self):
        """Offering a tool is not the same as requiring it; an unforced call may chat."""
        asked, poster, _ = provider((200, anthropic_body(GOOD)), vendor="anthropic")
        asked.ask(PROMPT)
        payload = poster.calls[0]["payload"]
        assert payload["tool_choice"] == {"type": "tool", "name": "respond"}
        assert payload["tools"][0]["input_schema"] == SCHEMA.as_json_schema()

    def test_the_system_and_user_halves_stay_separate(self):
        asked, poster, _ = provider((200, openai_body(GOOD)))
        asked.ask(PROMPT)
        messages = poster.calls[0]["payload"]["messages"]
        assert messages[0] == {"role": "system", "content": "be terse"}
        assert messages[1] == {"role": "user", "content": "is this a defect?"}

    def test_anthropic_takes_the_system_prompt_out_of_band(self):
        """Anthropic has a top-level system field; sending it as a message loses it."""
        asked, poster, _ = provider((200, anthropic_body(GOOD)), vendor="anthropic")
        asked.ask(PROMPT)
        assert poster.calls[0]["payload"]["system"] == "be terse"

    def test_each_vendor_gets_its_own_auth_header(self):
        asked, poster, _ = provider((200, openai_body(GOOD)))
        asked.ask(PROMPT)
        assert poster.calls[0]["headers"]["Authorization"].startswith("Bearer ")

        other, other_poster, _ = provider((200, anthropic_body(GOOD)), vendor="anthropic")
        other.ask(PROMPT)
        assert "x-api-key" in other_poster.calls[0]["headers"]
        assert "anthropic-version" in other_poster.calls[0]["headers"]

    def test_the_endpoints_differ_by_vendor(self):
        assert Endpoint("openai", "https://x/v1").url == "https://x/v1/chat/completions"
        assert Endpoint("anthropic", "https://x/v1").url == "https://x/v1/messages"

    def test_a_trailing_slash_does_not_produce_a_double_one(self):
        assert Endpoint("openai", "https://x/v1/").url == "https://x/v1/chat/completions"


class TestRetrying:
    @pytest.mark.parametrize("status", [408, 409, 429, 500, 502, 503, 504])
    def test_a_transient_status_is_retried(self, status: int):
        asked, poster, _ = provider((status, "busy"), (200, openai_body(GOOD)))
        assert isinstance(asked.ask(PROMPT), Answer)
        assert len(poster.calls) == 2

    def test_an_unreachable_host_is_retried(self):
        asked, poster, _ = provider(TransportError("connection refused"), (200, openai_body(GOOD)))
        assert isinstance(asked.ask(PROMPT), Answer)
        assert len(poster.calls) == 2

    def test_retrying_waits_longer_each_time(self):
        asked, _, clock = provider((429, "slow down"))
        asked.ask(PROMPT)
        assert clock.slept == [1.0, 2.0]

    def test_retries_are_bounded(self):
        """A service that is down must not become an unbounded loop."""
        asked, poster, _ = provider((503, "down"))
        reply = asked.ask(PROMPT)
        assert isinstance(reply, Declined)
        assert len(poster.calls) == MAX_ATTEMPTS

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
    def test_a_permanent_status_is_not_retried(self, status: int):
        """Retrying a malformed request wastes the budget and the wait."""
        asked, poster, clock = provider((status, "no"))
        assert isinstance(asked.ask(PROMPT), Declined)
        assert len(poster.calls) == 1
        assert clock.slept == []

    def test_a_schema_violation_is_not_retried(self):
        """A model that answered the wrong shape once will usually do it again."""
        asked, poster, _ = provider((200, openai_body({"verdict": "yes", "extra": "x"})))
        assert isinstance(asked.ask(PROMPT), Declined)
        assert len(poster.calls) == 1


class TestEveryFailureIsADecline:
    """djaudit is useful without a model; a network error must not end a run."""

    @pytest.mark.parametrize(
        ("responses", "expected"),
        [
            ([(401, '{"error":"bad key"}')], "401"),
            ([(500, "boom")], "500"),
            ([TransportError("timed out")], "unreachable"),
            ([(200, "not json at all")], "unparseable"),
            ([(200, "[1,2,3]")], "not an object"),
            ([(200, json.dumps({"choices": []}))], "no choices"),
            ([(200, json.dumps({"choices": [{"message": {}}]}))], "no content"),
            ([(200, openai_body(GOOD, choices=[{"finish_reason": "length"}]))], "truncated"),
            ([(200, json.dumps({"choices": [{"message": {"content": "{oops"}}]}))], "not JSON"),
        ],
    )
    def test_it_declines_with_a_reason(self, responses: list[Any], expected: str):
        asked, _, _ = provider(*responses)
        reply = asked.ask(PROMPT)
        assert isinstance(reply, Declined)
        assert expected in reply.reason

    def test_an_anthropic_reply_that_ignored_the_tool_is_a_decline(self):
        body = json.dumps({"content": [{"type": "text", "text": "I'd rather chat"}]})
        asked, _, _ = provider((200, body), vendor="anthropic")
        reply = asked.ask(PROMPT)
        assert isinstance(reply, Declined)
        assert "tool" in reply.reason

    def test_an_anthropic_truncation_is_a_decline(self):
        body = anthropic_body(GOOD, stop_reason="max_tokens")
        asked, _, _ = provider((200, body), vendor="anthropic")
        assert isinstance(asked.ask(PROMPT), Declined)

    def test_a_missing_credential_declines_before_any_call(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("TEST_KEY", raising=False)
        asked, poster, _ = provider((200, openai_body(GOOD)))
        reply = asked.ask(PROMPT)
        assert isinstance(reply, Declined)
        assert "TEST_KEY" in reply.reason
        assert poster.calls == []

    def test_the_control_a_good_reply_is_not_a_decline(self):
        """Every assertion above would pass on a provider that declined everything."""
        asked, _, _ = provider((200, openai_body(GOOD)))
        assert isinstance(asked.ask(PROMPT), Answer)


class TestTheKeyDoesNotEscape:
    """djaudit reports DJS-002. It must not commit it."""

    def test_a_vendor_that_echoes_the_key_back_does_not_get_it_printed(self, key: str):
        """Not hypothetical: several services include the header in a 401 body."""
        asked, _, _ = provider((401, f'{{"error":"invalid key {key}"}}'))
        reply = asked.ask(PROMPT)
        assert isinstance(reply, Declined)
        assert key not in reply.reason
        assert "[redacted]" in reply.reason

    def test_it_is_scrubbed_from_a_transport_error_too(self, key: str):
        asked, _, _ = provider(TransportError(f"failed to connect with {key}"))
        reply = asked.ask(PROMPT)
        assert isinstance(reply, Declined)
        assert key not in reply.reason

    def test_it_is_scrubbed_from_a_retryable_body(self, key: str):
        asked, _, _ = provider((429, f"rate limited for {key}"))
        reply = asked.ask(PROMPT)
        assert isinstance(reply, Declined)
        assert key not in reply.reason

    def test_the_control_an_unrelated_string_is_left_alone(self):
        """Scrubbing everything would pass every test above and hide the real errors."""
        asked, _, _ = provider((400, "model 'm' does not exist"))
        reply = asked.ask(PROMPT)
        assert isinstance(reply, Declined)
        assert "does not exist" in reply.reason

    def test_the_key_is_not_stored_on_the_provider(self, key: str):
        """It is read at the moment of use, so it cannot be pickled or repr'd out."""
        asked, _, _ = provider((200, openai_body(GOOD)))
        assert key not in repr(asked)

    def test_the_name_does_not_carry_the_key(self, key: str):
        asked, _, _ = provider((200, openai_body(GOOD)))
        assert asked.name == "openai:m"
        assert key not in asked.name


class TestBuilding:
    def test_it_builds_each_vendor(self):
        for vendor in ("openai", "anthropic"):
            assert build(vendor, "m", Credential("TEST_KEY")).endpoint.vendor == vendor

    def test_an_unknown_vendor_is_refused_at_construction(self):
        with pytest.raises(ValueError, match="unknown provider"):
            build("hal9000", "m", Credential("TEST_KEY"))

    def test_a_custom_base_url_is_honoured(self):
        """This is how a local vLLM or Ollama is reached without a code change."""
        built = build("openai", "m", Credential("TEST_KEY"), base_url="http://localhost:8000/v1")
        assert built.endpoint.url == "http://localhost:8000/v1/chat/completions"

    def test_a_non_http_base_url_is_refused(self):
        with pytest.raises(ValueError, match="http or https"):
            build("openai", "m", Credential("TEST_KEY"), base_url="file:///etc/passwd")

    def test_a_key_shaped_env_var_name_is_refused(self):
        """Credential already enforces this; asserted here because build() is the door."""
        with pytest.raises(Exception, match="looks like a key"):
            build("openai", "m", Credential("sk-ant-0123456789abcdefghij"))

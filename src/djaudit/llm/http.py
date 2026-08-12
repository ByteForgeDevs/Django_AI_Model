"""A provider that actually calls something.

Everything else in this package was written against ``NullProvider``, which
declines. This is the one module that opens a socket, and it is written so that
turning it on changes what djaudit *says* and never what djaudit *decides*: the
findings are still produced by 87 deterministic rules, and a model's reply is
still validated field by field against a schema the caller declared before
asking.

**Two vendors, one shape.** OpenAI-compatible services take a JSON Schema in
``response_format``; Anthropic takes the same schema as a tool's ``input_schema``
and is told to use that tool. Both are handed exactly what
``ResponseSchema.as_json_schema()`` renders, so adding a vendor means writing
an adapter here and nothing above this layer learns a vendor name. Anything
that speaks the OpenAI shape -- vLLM, Ollama, Together, Groq, OpenRouter, Azure
-- works by pointing ``base_url`` at it.

**Failure is a return value.** Every path out of this module that does not
produce a validated answer produces ``Declined``. Not an exception: djaudit's
whole claim is that it is useful without a model, and the way that claim rots
is a network error propagating out of the LLM layer and killing a run that had
already computed its findings. A timeout mid-audit should cost commentary, not
the audit.

**The key cannot reach an error message.** It is read from the environment at
the moment of use, put into a header, and never stored on the instance or
interpolated into any string this module builds. ``_scrub`` is the backstop for
the case that matters more: a vendor that echoes the key back in an error body,
which is not hypothetical. A tool that reports ``DJS-002`` for hardcoded
secrets and then prints one into a CI log would deserve everything it got.

**No new dependencies.** ``urllib.request`` is not a pleasant HTTP client, but
this is one POST with retries, and the alternative is putting httpx into a
package whose runtime dependency list is typer and rich.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from djaudit.llm.config import Credential
from djaudit.llm.provider import (
    Answer,
    Declined,
    Prompt,
    Reply,
    SchemaViolationError,
    Usage,
)

# A model writing a Django app produces several kilobytes of code, and a reply
# truncated mid-file is a syntax error rather than a short answer. Generous
# because the failure mode of too-small is silent corruption.
DEFAULT_MAX_TOKENS = 8192

# Long enough for a slow model on a long prompt, short enough that a wedged
# connection does not look like a hung tool.
DEFAULT_TIMEOUT = 120.0

# Retries are for the transient classes only. A 400 means the request was
# wrong and will be wrong again; retrying it wastes the budget and the wait.
RETRYABLE = frozenset({408, 409, 429, 500, 502, 503, 504})

MAX_ATTEMPTS = 3

# A vendor may ask for a wait longer than anyone wants to sit through. Honour
# Retry-After, but not unboundedly.
MAX_BACKOFF = 30.0

VENDORS = ("openai", "anthropic")

ANTHROPIC_VERSION = "2023-06-01"

# The tool Anthropic is told to call. The name is arbitrary and never leaves
# this module; it exists because Anthropic constrains output shape through
# tools rather than through a response format.
_TOOL_NAME = "respond"


class TransportError(Exception):
    """A call did not produce a usable body. Converted to ``Declined`` here."""


def _scrub(text: str, secret: str | None) -> str:
    """Remove the credential from anything about to be shown to a human.

    The provider is the party most likely to leak it -- several echo the
    Authorization header back inside a 401 body -- and that body is exactly
    what a user pastes into an issue.
    """
    if not secret:
        return text
    return text.replace(secret, "[redacted]")


def _post(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout: float,
) -> tuple[int, str]:
    """One POST. Returns the status and body; raises only on transport failure."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        # An HTTP error still carries a body, and the body is where a vendor
        # explains what was wrong with the request. Losing it would make every
        # 400 look identical.
        body = exc.read().decode("utf-8", "replace") if exc.fp is not None else ""
        return int(exc.code), body
    except urllib.error.URLError as exc:
        raise TransportError(str(exc.reason)) from exc
    except (TimeoutError, OSError) as exc:
        raise TransportError(str(exc)) from exc


def _backoff(attempt: int) -> float:
    """How long to wait before the next attempt.

    Exponential and capped. The vendor's own ``Retry-After`` would be better
    and is not available here -- ``_post`` returns the body rather than the
    headers -- so this is deliberately on the patient side of what one would
    ask for.
    """
    return min(MAX_BACKOFF, 2.0**attempt)


def _openai_payload(prompt: Prompt, model: str, max_tokens: int) -> dict[str, Any]:
    """The chat-completions shape, with structured output turned on.

    ``strict`` asks the vendor to enforce the schema during decoding, which
    turns a shape violation into a vendor-side error rather than an invalid
    reply. It is not relied on: ``ResponseSchema.validate`` runs on the result
    regardless, because a courtesy from a remote service is not a guarantee.
    """
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt.system},
            {"role": "user", "content": prompt.user},
        ],
        "max_completion_tokens": max_tokens,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "djaudit_reply",
                "strict": True,
                "schema": prompt.schema.as_json_schema(),
            },
        },
    }


def _anthropic_payload(prompt: Prompt, model: str, max_tokens: int) -> dict[str, Any]:
    """The messages shape. Structure is constrained through a forced tool call."""
    return {
        "model": model,
        "max_tokens": max_tokens,
        "system": prompt.system,
        "messages": [{"role": "user", "content": prompt.user}],
        "tools": [
            {
                "name": _TOOL_NAME,
                "description": "Return the answer in the required shape.",
                "input_schema": prompt.schema.as_json_schema(),
            }
        ],
        "tool_choice": {"type": "tool", "name": _TOOL_NAME},
    }


def _openai_reply(body: dict[str, Any]) -> tuple[object, Usage]:
    """Pull the payload and the cost out of a chat completion."""
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise TransportError("reply carried no choices")

    message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
    if choices[0].get("finish_reason") == "length":
        # A truncated JSON object usually fails to parse, but a short schema
        # can be cut at a point that still parses and silently loses a field.
        raise TransportError("reply was truncated by max_tokens")

    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise TransportError("reply carried no content")

    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise TransportError(f"reply was not JSON: {exc}") from exc

    usage = body.get("usage", {})
    return payload, Usage(
        input_tokens=int(usage.get("prompt_tokens", 0) or 0),
        output_tokens=int(usage.get("completion_tokens", 0) or 0),
    )


def _anthropic_reply(body: dict[str, Any]) -> tuple[object, Usage]:
    """Pull the payload out of the forced tool call."""
    if body.get("stop_reason") == "max_tokens":
        raise TransportError("reply was truncated by max_tokens")

    blocks = body.get("content")
    if not isinstance(blocks, list):
        raise TransportError("reply carried no content")

    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            usage = body.get("usage", {})
            return block.get("input"), Usage(
                input_tokens=int(usage.get("input_tokens", 0) or 0),
                output_tokens=int(usage.get("output_tokens", 0) or 0),
            )
    raise TransportError("reply did not use the tool it was told to use")


@dataclass(frozen=True)
class Endpoint:
    """Where a vendor lives and how it is spoken to."""

    vendor: str
    base_url: str

    def __post_init__(self) -> None:
        if self.vendor not in VENDORS:
            raise ValueError(
                f"unknown vendor {self.vendor!r}; expected one of {', '.join(VENDORS)}"
            )
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError(f"base_url must be http or https, got {self.base_url!r}")

    @property
    def url(self) -> str:
        base = self.base_url.rstrip("/")
        return f"{base}/messages" if self.vendor == "anthropic" else f"{base}/chat/completions"

    def headers(self, key: str) -> dict[str, str]:
        if self.vendor == "anthropic":
            return {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}
        return {"Authorization": f"Bearer {key}"}

    def payload(self, prompt: Prompt, model: str, max_tokens: int) -> dict[str, Any]:
        if self.vendor == "anthropic":
            return _anthropic_payload(prompt, model, max_tokens)
        return _openai_payload(prompt, model, max_tokens)

    def parse(self, body: dict[str, Any]) -> tuple[object, Usage]:
        if self.vendor == "anthropic":
            return _anthropic_reply(body)
        return _openai_reply(body)


DEFAULT_BASE = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
}


@dataclass
class HTTPProvider:
    """Asks a real model, and declines rather than raising when it cannot.

    ``poster`` and ``sleeper`` are injected so the tests can drive every
    failure branch without a network or a wait. A retry policy that is only
    exercised against a live service is a retry policy nobody has run.
    """

    endpoint: Endpoint
    model: str
    credential: Credential
    max_tokens: int = DEFAULT_MAX_TOKENS
    timeout: float = DEFAULT_TIMEOUT
    poster: Callable[[str, dict[str, Any], dict[str, str], float], tuple[int, str]] = field(
        default=_post
    )
    sleeper: Callable[[float], None] = time.sleep

    @property
    def name(self) -> str:
        return f"{self.endpoint.vendor}:{self.model}"

    def ask(self, prompt: Prompt) -> Reply:
        key = self.credential.resolve()
        if key is None:
            return Declined(f"${self.credential.env_var} is not set in the environment")

        headers = self.endpoint.headers(key)

        last = "no attempt was made"
        for attempt in range(MAX_ATTEMPTS):
            outcome = self._attempt(prompt, headers, key)
            if isinstance(outcome, Answer):
                return outcome
            last, retryable = outcome
            if not retryable or attempt == MAX_ATTEMPTS - 1:
                break
            self.sleeper(_backoff(attempt))
        return Declined(last)

    def _attempt(
        self,
        prompt: Prompt,
        headers: dict[str, str],
        key: str,
    ) -> Answer | tuple[str, bool]:
        """One call. Returns an answer, or a reason and whether to try again.

        The reply is validated against ``prompt.schema`` -- the same object
        that was rendered into the request a few lines above -- so validation
        is provably against what the model was actually shown.
        """
        payload = self.endpoint.payload(prompt, self.model, self.max_tokens)
        try:
            status, body = self.poster(self.endpoint.url, payload, headers, self.timeout)
        except TransportError as exc:
            return f"{self.endpoint.vendor} was unreachable: {_scrub(str(exc), key)}", True

        if status in RETRYABLE:
            return f"{self.endpoint.vendor} returned {status}: {_scrub(body[:200], key)}", True
        if status >= 400:
            return f"{self.endpoint.vendor} returned {status}: {_scrub(body[:400], key)}", False

        try:
            decoded = json.loads(body)
        except json.JSONDecodeError as exc:
            return f"{self.endpoint.vendor} returned unparseable JSON: {exc}", False
        if not isinstance(decoded, dict):
            return f"{self.endpoint.vendor} returned {type(decoded).__name__}, not an object", False

        try:
            raw, usage = self.endpoint.parse(decoded)
        except TransportError as exc:
            return _scrub(str(exc), key), False

        try:
            content = prompt.schema.validate(raw)
        except SchemaViolationError as exc:
            # Not retried. A model that answered the wrong shape once will
            # usually answer it again, and the budget is better spent
            # elsewhere. The violation is reported rather than repaired.
            return f"reply did not match the declared schema: {_scrub(str(exc), key)}", False

        return Answer(content=content, model=self.model, usage=usage)


def build(
    vendor: str,
    model: str,
    credential: Credential,
    base_url: str = "",
) -> HTTPProvider:
    """Assemble a provider for a vendor, or raise if it is not one we speak."""
    if vendor not in VENDORS:
        raise ValueError(f"unknown provider {vendor!r}; expected one of {', '.join(VENDORS)}")
    endpoint = Endpoint(vendor=vendor, base_url=base_url or DEFAULT_BASE[vendor])
    return HTTPProvider(endpoint=endpoint, model=model, credential=credential)

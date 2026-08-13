"""Where the settings come from, and where the key does not.

djaudit spends its life reporting projects that keep secrets where secrets
should not be kept. `DJS-002` through `DJS-005` exist for exactly that. A tool
that then invites you to paste an API key into its own config file would be
worth ignoring, so this module refuses to accept one: a key is named by the
environment variable that holds it, never written down, and a config file that
inlines something key-shaped is an error that names the rule it would violate.

The default is offline. Not "offline if we cannot find credentials" -- offline
until asked, so that installing djaudit never causes source code to leave a
machine because an environment variable happened to be set in CI.
"""

from __future__ import annotations

import ipaddress
import os
import re
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

DEFAULT_CACHE_DIR = Path(".djaudit-cache") / "llm"

# Long, high-entropy, and shaped like the things vendors hand out. Deliberately
# broad: the cost of a false positive here is a clear error message telling
# someone to use an environment variable, which is advice they should take
# whether or not the string was really a key.
KEY_SHAPED = re.compile(
    r"""
    (sk-[A-Za-z0-9_\-]{16,})       # OpenAI and several imitators
    | (sk-ant-[A-Za-z0-9_\-]{16,}) # Anthropic
    | (AIza[A-Za-z0-9_\-]{20,})    # Google
    | ([A-Za-z0-9_\-]{40,})        # anything else long enough to be one
    """,
    re.VERBOSE,
)


class ConfigError(Exception):
    """A configuration this tool will not run with."""


def is_loopback(base_url: str) -> bool:
    """Whether this URL can only reach a server on this machine.

    The credential requirement exists to stop source code leaving a machine
    unnoticed, so it is about egress rather than about authentication. A model
    served on loopback has no egress to guard: ollama, llama.cpp, vLLM and LM
    Studio all listen locally and none of them issue an API key, so demanding
    one would mean djaudit could talk to a vendor but not to the model running
    on the user's own laptop.

    Deliberately strict about what counts. Only a literal loopback address or
    the exact name ``localhost`` qualifies, and every other host -- including
    a private address like ``10.0.0.5`` and any name that merely happens to
    resolve to 127.0.0.1 -- still needs a credential. Names are not resolved:
    a resolver answer is a runtime fact that can differ from the one seen here,
    and the safe direction for that uncertainty is to keep asking for a key.
    """
    host = urlparse(base_url.strip()).hostname
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True, slots=True)
class Credential:
    """A key, held by the name of the variable that carries it.

    The value is read at the moment of use and is not stored on this object.
    Two things follow, and both are the point: the key cannot be serialised
    into a cache entry or a log line by accident, and rotating it does not
    require restarting anything.
    """

    env_var: str

    def __post_init__(self) -> None:
        if not self.env_var:
            raise ConfigError("a credential needs the name of an environment variable")
        if KEY_SHAPED.fullmatch(self.env_var.strip()):
            raise ConfigError(
                f"{self.env_var[:6]}... looks like a key rather than a variable name. "
                "Name the variable that holds it -- djaudit reports projects that "
                "keep secrets in source (DJS-002) and will not keep one itself."
            )

    def resolve(self) -> str | None:
        value = os.environ.get(self.env_var)
        return value if value else None

    @property
    def present(self) -> bool:
        return self.resolve() is not None


@dataclass(frozen=True, slots=True)
class LLMConfig:
    """Everything the layer needs, and nothing it should not hold.

    ``offline`` is the field that decides, and it defaults to True. A provider
    name and a model can be configured freely; neither causes a call.
    """

    offline: bool = True
    provider: str = "null"
    model: str = ""
    credential: Credential | None = None
    cache_dir: Path = DEFAULT_CACHE_DIR
    max_tokens: int = 0
    max_calls: int = 0
    timeout: float = 0.0
    extras: dict[str, str] = field(default_factory=dict)

    @property
    def local(self) -> bool:
        """Whether this config points at a model on this machine."""
        return is_loopback(self.extras.get("base_url", ""))

    @property
    def usable(self) -> tuple[bool, str]:
        """Whether a call may be attempted, and if not, which absence this is.

        Returned as a reason rather than a bare False because "you did not ask
        for a model", "you asked for one and named no provider" and "the key is
        not in the environment" are three different problems, and a user who is
        told only that nothing happened will debug the wrong one.
        """
        if self.offline:
            return False, "offline (pass --llm to enable)"
        if self.provider in {"", "null"}:
            return False, "no provider configured"
        if self.credential is None:
            if self.local:
                return True, ""
            return False, f"provider {self.provider!r} needs a credential and none is configured"
        if not self.credential.present:
            return False, f"${self.credential.env_var} is not set in the environment"
        return True, ""

    def going_offline(self, reason: str) -> LLMConfig:
        """Degrade, keeping the reason for the report."""
        return replace(self, offline=True, extras={**self.extras, "degraded": reason})


def _reject_inline_secret(table: dict[str, Any], where: Path) -> None:
    for key, value in table.items():
        if not isinstance(value, str):
            continue
        if key in {"api_key", "key", "token", "secret", "password"}:
            raise ConfigError(
                f"{where}: [tool.djaudit.llm] must not contain {key!r}. "
                f'Use api_key_env = "YOUR_VAR_NAME" and keep the value in the '
                f"environment. djaudit reports this exact mistake as DJS-002."
            )
        if KEY_SHAPED.fullmatch(value.strip()):
            raise ConfigError(
                f"{where}: the value of {key!r} looks like a credential. "
                f"Keep it in the environment and name the variable instead."
            )


def from_pyproject(path: Path) -> LLMConfig:
    """Read ``[tool.djaudit.llm]``, or return the offline default.

    A missing file, a missing table and an empty table are all the same answer:
    no model. Only a malformed one is an error, because that is a mistake
    rather than a choice.
    """
    if not path.is_file():
        return LLMConfig()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc

    table = data.get("tool", {}).get("djaudit", {}).get("llm", {})
    if not isinstance(table, dict) or not table:
        return LLMConfig()

    _reject_inline_secret(table, path)

    env_var = table.get("api_key_env", "")
    cache = table.get("cache_dir")

    # A base_url that is read but not honoured is worse than one that is
    # rejected: someone pointing djaudit at a self-hosted endpoint would have
    # their source sent to the public vendor instead, and nothing would say so.
    extras: dict[str, str] = {}
    base_url = str(table.get("base_url", "")).strip()
    if base_url:
        if not base_url.startswith(("http://", "https://")):
            raise ConfigError(f"{path}: base_url must be an http or https URL, got {base_url!r}")
        extras["base_url"] = base_url

    return LLMConfig(
        # A config file may describe a provider without turning one on. Only an
        # explicit `enabled = true` -- or the CLI flag -- takes this offline
        # flag down, so checking a repo out does not start making calls.
        offline=not bool(table.get("enabled", False)),
        provider=str(table.get("provider", "null")),
        model=str(table.get("model", "")),
        credential=Credential(str(env_var)) if env_var else None,
        cache_dir=Path(str(cache)) if cache else DEFAULT_CACHE_DIR,
        max_tokens=int(table.get("max_tokens", 0)),
        max_calls=int(table.get("max_calls", 0)),
        timeout=float(table.get("timeout", 0.0)),
        extras=extras,
    )


def resolve(
    path: Path | None = None,
    *,
    enable: bool | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> LLMConfig:
    """File first, then explicit flags, which win.

    ``--no-llm`` has to be able to override a config file that enables one,
    because the person typing it is at the machine and the file is not.
    """
    config = from_pyproject(path) if path is not None else LLMConfig()
    if provider is not None:
        config = replace(config, provider=provider)
    if model is not None:
        config = replace(config, model=model)
    if enable is not None:
        config = replace(config, offline=not enable)
    return config

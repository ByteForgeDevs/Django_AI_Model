"""What a provider is, and what it is structurally unable to say.

Two problems are solved here, and only one of them is transport.

The first is vendor independence. A provider is a protocol with one method, and
the request carries its own output schema in a small vendor-neutral form that
renders to the JSON Schema dialect OpenAI's structured outputs and Anthropic's
tool inputs both accept. Adding a vendor means writing an adapter, not editing
this file, and nothing above this layer names one.

The second is the one that matters. This project's findings come from
deterministic rules with evidence attached, and a model must not be able to
change that set. Saying so in a document is worth very little -- a rule written
down is obeyed until someone is in a hurry, and the whole value of the tool is
gone the first time a model talks it out of a real finding.

So the guarantee is made out of types rather than discipline. A caller declares
the fields it will accept before it asks anything. The reply is validated
against that declaration, and a field the caller did not declare is a
``SchemaViolationError`` rather than an ignored extra key. There is therefore no way
for a model to return ``{"is_defect": false}`` and have it reach anything,
because no caller in this codebase declares such a field and an undeclared one
does not survive ``ResponseSchema.validate``.

Rejecting rather than dropping is the deliberate part. Silently ignoring the
unknown key would also keep it out of the finding list, and would leave nothing
behind when a provider starts returning something nobody expected. A violation
is loud, and ``6.5.4`` is a test that makes a hostile provider try.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class SchemaViolationError(Exception):
    """A reply did not match the schema its prompt declared.

    Raised rather than repaired. A provider that returns the wrong shape is
    either misconfigured or answering a different question, and both are worth
    surfacing rather than papering over with a default.
    """


class FieldKind(StrEnum):
    """The value types a reply may carry.

    Deliberately three. Every question this project asks a model has an answer
    that is a word from a closed set, a sentence, a number, or a flag, and a
    schema language rich enough to express nested objects is rich enough to
    express a structure nobody validated.
    """

    STRING = "string"
    INTEGER = "integer"
    BOOLEAN = "boolean"


@dataclass(frozen=True, slots=True)
class Field:
    """One value a caller is willing to receive.

    ``choices`` closes a string field to a fixed set, which is how every
    judgement in this package is constrained: a model choosing among words the
    caller wrote cannot invent a fourth.
    """

    name: str
    kind: FieldKind
    description: str
    choices: tuple[str, ...] = ()
    required: bool = True

    def __post_init__(self) -> None:
        if self.choices and self.kind is not FieldKind.STRING:
            raise ValueError(f"{self.name}: choices only apply to string fields")
        if not self.name:
            raise ValueError("a field needs a name")

    def as_json_schema(self) -> dict[str, Any]:
        schema: dict[str, Any] = {"type": self.kind.value, "description": self.description}
        if self.choices:
            schema["enum"] = list(self.choices)
        return schema


_PYTHON_TYPES: dict[FieldKind, type | tuple[type, ...]] = {
    FieldKind.STRING: str,
    FieldKind.INTEGER: int,
    FieldKind.BOOLEAN: bool,
}


@dataclass(frozen=True, slots=True)
class ResponseSchema:
    """The complete set of fields a caller will accept, and nothing else."""

    fields: tuple[Field, ...]

    def __post_init__(self) -> None:
        names = [f.name for f in self.fields]
        if len(names) != len(set(names)):
            raise ValueError("duplicate field name in schema")
        if not names:
            raise ValueError("a schema with no fields cannot constrain anything")

    def as_json_schema(self) -> dict[str, Any]:
        """Render to the dialect OpenAI and Anthropic both accept.

        ``additionalProperties: false`` asks the vendor to enforce what
        ``validate`` enforces anyway. It is a courtesy to the provider and to
        the token budget, not a thing this code trusts: the check below runs on
        every reply regardless of what the vendor promised.
        """
        return {
            "type": "object",
            "properties": {f.name: f.as_json_schema() for f in self.fields},
            "required": [f.name for f in self.fields if f.required],
            "additionalProperties": False,
        }

    def validate(self, payload: object) -> dict[str, Any]:
        """Return the payload as declared, or raise.

        Never returns a key the caller did not ask for. This is the whole of
        the structural guarantee, so it is written to be read.
        """
        if not isinstance(payload, dict):
            raise SchemaViolationError(f"expected an object, got {type(payload).__name__}")

        declared = {f.name: f for f in self.fields}
        unknown = sorted(set(payload) - set(declared))
        if unknown:
            raise SchemaViolationError(
                f"reply carries {len(unknown)} field(s) the caller never declared: "
                f"{', '.join(unknown)}. A provider cannot widen the question it was asked."
            )

        clean: dict[str, Any] = {}
        for name, field in declared.items():
            if name not in payload:
                if field.required:
                    raise SchemaViolationError(f"reply is missing required field {name!r}")
                continue
            value = payload[name]
            # bool is a subclass of int, so an integer field must refuse True.
            if field.kind is FieldKind.INTEGER and isinstance(value, bool):
                raise SchemaViolationError(f"{name!r}: expected integer, got bool")
            if not isinstance(value, _PYTHON_TYPES[field.kind]):
                raise SchemaViolationError(
                    f"{name!r}: expected {field.kind.value}, got {type(value).__name__}"
                )
            if field.choices and value not in field.choices:
                raise SchemaViolationError(
                    f"{name!r}: {value!r} is not one of {', '.join(field.choices)}"
                )
            clean[name] = value
        return clean


@dataclass(frozen=True, slots=True)
class Prompt:
    """One question, carrying the shape of the answer it will accept.

    ``version`` is part of the cache key. A prompt whose wording changed is a
    different question, and an answer to the old one is not an answer to the
    new one however cheap it would be to reuse.
    """

    version: str
    system: str
    user: str
    schema: ResponseSchema

    def __post_init__(self) -> None:
        if not self.version:
            raise ValueError("a prompt must be versioned, or its cache entries outlive it")


@dataclass(frozen=True, slots=True)
class Usage:
    """What an answer cost, so a budget can be enforced against measurement."""

    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class Answer:
    """A validated reply. ``content`` holds exactly the declared fields."""

    content: dict[str, Any]
    model: str
    usage: Usage = Usage()
    cached: bool = False


@dataclass(frozen=True, slots=True)
class Declined:
    """No answer, and why.

    Declining is a normal outcome, not an error: no credentials, offline by
    choice, budget spent, provider unreachable. It is a return value rather
    than an exception so that every caller has to decide what to do without a
    model, which is the case this tool must remain useful in.
    """

    reason: str


Reply = Answer | Declined


@runtime_checkable
class Provider(Protocol):
    """Anything that can be asked a schema-constrained question."""

    @property
    def name(self) -> str: ...

    def ask(self, prompt: Prompt) -> Reply: ...


@dataclass(frozen=True, slots=True)
class NullProvider:
    """The default, and the one CI uses.

    djaudit works without a model. This provider is what makes that the tested
    path rather than the theoretical one: every consumer is written against a
    provider that always declines, so degradation is exercised on every run
    instead of only when someone's key expires.
    """

    reason: str = "no model configured"

    @property
    def name(self) -> str:
        return "null"

    def ask(self, prompt: Prompt) -> Reply:
        return Declined(self.reason)

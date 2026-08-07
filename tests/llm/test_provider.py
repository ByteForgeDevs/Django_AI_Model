"""What a provider may return, and what it cannot make anyone believe.

The interesting tests here are the refusals. A schema that accepts a field
nobody declared is a schema that cannot carry the one guarantee this package
makes, so most of this file is about the rejection path.
"""

from __future__ import annotations

import pytest

from djaudit.llm import (
    Answer,
    Declined,
    Field,
    FieldKind,
    NullProvider,
    Prompt,
    Provider,
    ResponseSchema,
    SchemaViolationError,
    Usage,
)

PRIORITY = Field(
    name="priority",
    kind=FieldKind.STRING,
    description="how soon a reviewer should look",
    choices=("now", "soon", "whenever"),
)
REASON = Field(name="reason", kind=FieldKind.STRING, description="why")
RANK = Field(name="rank", kind=FieldKind.INTEGER, description="position")


def schema(*fields: Field) -> ResponseSchema:
    return ResponseSchema(fields=fields)


class TestWhatTheSchemaAccepts:
    def test_a_reply_matching_the_declaration(self):
        got = schema(PRIORITY, REASON).validate({"priority": "now", "reason": "reachable"})
        assert got == {"priority": "now", "reason": "reachable"}

    def test_an_optional_field_may_be_absent(self):
        optional = Field(name="reason", kind=FieldKind.STRING, description="why", required=False)
        assert schema(PRIORITY, optional).validate({"priority": "soon"}) == {"priority": "soon"}

    def test_an_integer_field(self):
        assert schema(RANK).validate({"rank": 3}) == {"rank": 3}

    def test_a_boolean_field(self):
        flag = Field(name="certain", kind=FieldKind.BOOLEAN, description="sure?")
        assert schema(flag).validate({"certain": False}) == {"certain": False}


class TestWhatTheSchemaRefuses:
    """The guarantee, stated as the things that cannot get through."""

    def test_a_field_the_caller_never_declared(self):
        """The one that matters.

        A model deciding a finding is not a defect has to get that opinion into
        a caller somehow, and this is the door. It does not open, and it does
        not open quietly either -- dropping the key would keep it out of the
        finding list while leaving no trace that a provider had started
        answering a different question.
        """
        with pytest.raises(SchemaViolationError, match="never declared"):
            schema(PRIORITY).validate({"priority": "now", "is_defect": False})

    def test_and_the_message_names_it(self):
        with pytest.raises(SchemaViolationError, match="is_defect"):
            schema(PRIORITY).validate({"priority": "now", "is_defect": False})

    def test_a_value_outside_the_closed_set(self):
        with pytest.raises(SchemaViolationError, match="not one of"):
            schema(PRIORITY).validate({"priority": "urgent"})

    def test_a_missing_required_field(self):
        with pytest.raises(SchemaViolationError, match="missing required"):
            schema(PRIORITY, REASON).validate({"priority": "now"})

    def test_the_wrong_type(self):
        with pytest.raises(SchemaViolationError, match="expected integer"):
            schema(RANK).validate({"rank": "3"})

    def test_a_bool_offered_as_an_integer(self):
        """`bool` subclasses `int`, so the obvious isinstance check admits it."""
        with pytest.raises(SchemaViolationError, match="expected integer, got bool"):
            schema(RANK).validate({"rank": True})

    def test_something_that_is_not_an_object(self):
        with pytest.raises(SchemaViolationError, match="expected an object"):
            schema(PRIORITY).validate(["now"])

    def test_a_schema_with_no_fields_is_refused_at_construction(self):
        """It would accept `{}` and nothing else, which constrains nothing."""
        with pytest.raises(ValueError, match="cannot constrain"):
            ResponseSchema(fields=())

    def test_a_duplicate_field_name(self):
        with pytest.raises(ValueError, match="duplicate"):
            schema(PRIORITY, PRIORITY)

    def test_choices_on_a_non_string_field(self):
        with pytest.raises(ValueError, match="only apply to string"):
            Field(name="rank", kind=FieldKind.INTEGER, description="n", choices=("1",))


class TestTheRenderedJsonSchema:
    """Rendered once, for every vendor. Checked here because a wrong render is
    a runtime error at someone else's API, which is the worst place to find it.
    """

    def test_it_is_a_closed_object(self):
        rendered = schema(PRIORITY, REASON).as_json_schema()
        assert rendered["type"] == "object"
        assert rendered["additionalProperties"] is False

    def test_required_lists_only_required_fields(self):
        optional = Field(name="note", kind=FieldKind.STRING, description="x", required=False)
        rendered = schema(PRIORITY, optional).as_json_schema()
        assert rendered["required"] == ["priority"]

    def test_a_closed_set_renders_as_an_enum(self):
        rendered = schema(PRIORITY).as_json_schema()
        assert rendered["properties"]["priority"]["enum"] == ["now", "soon", "whenever"]

    def test_descriptions_survive(self):
        """They are the only instruction the model gets about a field."""
        rendered = schema(REASON).as_json_schema()
        assert rendered["properties"]["reason"]["description"] == "why"


class TestThePrompt:
    def test_a_prompt_must_be_versioned(self):
        """An unversioned prompt cannot be cached safely, so it cannot exist."""
        with pytest.raises(ValueError, match="versioned"):
            Prompt(version="", system="s", user="u", schema=schema(PRIORITY))

    def test_a_versioned_prompt_is_hashable(self):
        """It becomes part of a cache key, so it has to be."""
        prompt = Prompt(version="1", system="s", user="u", schema=schema(PRIORITY))
        assert hash(prompt) == hash(prompt)


class TestTheNullProvider:
    """The path CI takes, so it is the path that must work."""

    def test_it_declines(self):
        prompt = Prompt(version="1", system="s", user="u", schema=schema(PRIORITY))
        reply = NullProvider().ask(prompt)
        assert isinstance(reply, Declined)
        assert reply.reason == "no model configured"

    def test_it_satisfies_the_protocol(self):
        assert isinstance(NullProvider(), Provider)

    def test_the_reason_can_say_which_absence_this_is(self):
        """ "No key" and "offline by choice" are different, and users ask which."""
        assert NullProvider(reason="offline by request").ask(
            Prompt(version="1", system="s", user="u", schema=schema(PRIORITY))
        ) == Declined("offline by request")


class TestUsage:
    def test_total_adds_both_directions(self):
        assert Usage(input_tokens=100, output_tokens=25).total == 125

    def test_an_answer_defaults_to_no_recorded_cost(self):
        """A recorded or cached answer costs nothing, and must not be counted."""
        assert Answer(content={"priority": "now"}, model="fake").usage.total == 0

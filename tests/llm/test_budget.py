"""What a run does when it runs out.

The property under test is not arithmetic. It is that exhaustion degrades to
the deterministic path -- the same path ``NullProvider`` exercises on every
commit -- rather than to a truncated or a differently-shaped result. So the
tests care most about what happens *after* the budget is gone: that asking
again is refused rather than raising, that the refusal says which limit was
hit, and that no call escapes accounting.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from djaudit.llm.budget import (
    Budget,
    Metered,
    RateLimit,
    estimate_prompt,
    estimate_tokens,
)
from djaudit.llm.provider import (
    Answer,
    Declined,
    Field,
    FieldKind,
    NullProvider,
    Prompt,
    Reply,
    ResponseSchema,
    Usage,
)

SCHEMA = ResponseSchema(
    fields=(Field("verdict", FieldKind.STRING, "real or noise", choices=("real", "noise")),)
)
PROMPT = Prompt(version="v1", system="You review findings.", user="Is this real?", schema=SCHEMA)


@dataclass
class CountingProvider:
    reply: Reply
    calls: int = 0
    name: str = "counting"

    def ask(self, prompt: Prompt) -> Reply:
        self.calls += 1
        return self.reply


def answer(tokens: int = 100, *, cached: bool = False) -> Answer:
    return Answer(
        content={"verdict": "real"},
        model="m",
        usage=Usage(input_tokens=tokens, output_tokens=0),
        cached=cached,
    )


class TestTheEstimateRoundsAgainstUs:
    """A budget enforced against a low guess is a budget that is exceeded."""

    def test_an_estimate_is_at_least_a_quarter_of_the_characters(self) -> None:
        text = "x" * 400
        assert estimate_tokens(text) >= 100

    def test_the_estimate_beats_the_usual_four_character_rule(self) -> None:
        """Code is denser than prose, so the prose ratio under-counts it."""
        text = "obj.related.field_name" * 20
        assert estimate_tokens(text) > len(text) // 4

    def test_a_short_string_still_costs_something(self) -> None:
        assert estimate_tokens("a") == 1

    def test_an_empty_string_costs_nothing(self) -> None:
        assert estimate_tokens("") == 0

    def test_the_schema_is_counted_too(self) -> None:
        """It is sent with the request, and on a small prompt it is not noise."""
        bare = Prompt(
            version="v1",
            system="s",
            user="u",
            schema=ResponseSchema(fields=(Field("a", FieldKind.STRING, ""),)),
        )
        rich = Prompt(
            version="v1",
            system="s",
            user="u",
            schema=ResponseSchema(
                fields=(
                    Field("a", FieldKind.STRING, "a much longer description here"),
                    Field("b", FieldKind.STRING, "another", choices=("x", "y", "z")),
                )
            ),
        )
        assert estimate_prompt(rich) > estimate_prompt(bare)


class TestTheLimitIsEnforced:
    def test_calls_stop_at_the_call_ceiling(self) -> None:
        inner = CountingProvider(answer())
        metered = Metered(inner, Budget(max_calls=2))

        replies = [metered.ask(PROMPT) for _ in range(5)]

        assert inner.calls == 2
        assert sum(isinstance(r, Answer) for r in replies) == 2

    def test_calls_stop_at_the_token_ceiling(self) -> None:
        inner = CountingProvider(answer(tokens=100))
        metered = Metered(inner, Budget(max_tokens=250))

        for _ in range(5):
            metered.ask(PROMPT)

        assert inner.calls == 3
        assert metered.budget.spent_tokens == 300

    def test_a_zero_ceiling_means_unlimited(self) -> None:
        """The contrast. A limiter that refuses everything passes every test
        above and makes the whole layer dead code."""
        inner = CountingProvider(answer())
        metered = Metered(inner, Budget())

        for _ in range(20):
            metered.ask(PROMPT)

        assert inner.calls == 20
        assert metered.budget.unlimited

    def test_a_call_that_will_not_fit_is_refused_before_it_is_made(self) -> None:
        """Not after. Charging for a call we knew would overrun is the bug the
        estimate exists to prevent."""
        inner = CountingProvider(answer(tokens=100))
        metered = Metered(inner, Budget(max_tokens=5))

        reply = metered.ask(PROMPT)

        assert inner.calls == 0
        assert isinstance(reply, Declined)


class TestExhaustionIsNotAnError:
    def test_running_out_returns_a_refusal(self) -> None:
        metered = Metered(CountingProvider(answer()), Budget(max_calls=1))
        metered.ask(PROMPT)

        spent = metered.ask(PROMPT)

        assert isinstance(spent, Declined)

    def test_running_out_does_not_raise(self) -> None:
        """Exhaustion travels the same route as having no model at all, which
        is the route CI exercises on every commit."""
        metered = Metered(NullProvider(), Budget(max_calls=1))

        for _ in range(10):
            assert isinstance(metered.ask(PROMPT), Declined)

    def test_the_refusal_names_which_limit(self) -> None:
        calls = Metered(CountingProvider(answer()), Budget(max_calls=1))
        calls.ask(PROMPT)
        tokens = Metered(CountingProvider(answer(tokens=100)), Budget(max_tokens=5))

        call_reason = calls.ask(PROMPT)
        token_reason = tokens.ask(PROMPT)

        assert isinstance(call_reason, Declined)
        assert isinstance(token_reason, Declined)
        assert "call budget" in call_reason.reason
        assert "token budget" in token_reason.reason

    def test_the_token_refusal_shows_the_arithmetic(self) -> None:
        """A user told only "budget spent" cannot tell whether to raise the
        limit by ten percent or by ten times."""
        metered = Metered(CountingProvider(answer(tokens=100)), Budget(max_tokens=5))

        reply = metered.ask(PROMPT)

        assert isinstance(reply, Declined)
        assert "0/5 tokens" in reply.reason
        assert "next call needs about" in reply.reason


class TestTheCeilingIsOnWhatStarts:
    """A stated limitation, tested so that it stays the one it is.

    The check runs before a call and a response's size cannot be known in
    advance, so a ceiling can be overshot -- but by at most one response, and
    never by a second call that was started after the limit was known to be
    gone.
    """

    def test_a_single_response_may_overshoot(self) -> None:
        metered = Metered(CountingProvider(answer(tokens=10_000)), Budget(max_tokens=100))

        metered.ask(PROMPT)

        assert metered.budget.spent_tokens > metered.budget.max_tokens

    def test_no_further_call_starts_after_the_overshoot(self) -> None:
        inner = CountingProvider(answer(tokens=10_000))
        metered = Metered(inner, Budget(max_tokens=100))

        for _ in range(5):
            metered.ask(PROMPT)

        assert inner.calls == 1


class TestNothingEscapesAccounting:
    def test_a_provider_reporting_no_usage_is_charged_the_estimate(self) -> None:
        """Otherwise a provider that omits usage gets an unlimited budget."""
        inner = CountingProvider(Answer(content={"verdict": "real"}, model="m", usage=Usage()))
        metered = Metered(inner, Budget(max_tokens=1000))

        metered.ask(PROMPT)

        assert metered.budget.spent_tokens > 0

    def test_a_cache_hit_is_charged_nothing(self) -> None:
        inner = CountingProvider(answer(tokens=0, cached=True))
        metered = Metered(inner, Budget(max_tokens=1000))

        metered.ask(PROMPT)

        assert metered.budget.spent_tokens == 0
        assert metered.budget.spent_calls == 0

    def test_a_cache_hit_does_not_consume_the_call_ceiling(self) -> None:
        """A cached run must not be limited to the same number of findings a
        fresh one was."""
        inner = CountingProvider(answer(tokens=0, cached=True))
        metered = Metered(inner, Budget(max_calls=2))

        for _ in range(10):
            assert isinstance(metered.ask(PROMPT), Answer)

    def test_refusals_are_counted_separately_from_spend(self) -> None:
        """So a summary can say "40 of 200 findings were reviewed" instead of
        silently reporting on 40."""
        metered = Metered(CountingProvider(answer()), Budget(max_calls=2))

        for _ in range(5):
            metered.ask(PROMPT)

        assert metered.budget.spent_calls == 2
        assert metered.budget.declined_calls == 3

    def test_a_providers_own_refusal_is_counted_as_one(self) -> None:
        metered = Metered(CountingProvider(Declined("unreachable")), Budget())

        metered.ask(PROMPT)

        assert metered.budget.declined_calls == 1
        assert metered.budget.spent_calls == 0

    def test_remaining_is_none_when_unlimited(self) -> None:
        assert Budget().remaining_tokens() is None
        assert Budget(max_tokens=100).remaining_tokens() == 100

    def test_remaining_never_goes_negative(self) -> None:
        budget = Budget(max_tokens=100)
        budget.charge(Usage(input_tokens=250))

        assert budget.remaining_tokens() == 0


class TestSwitchingOffMidRun:
    def test_going_offline_refuses_everything_after(self) -> None:
        inner = CountingProvider(answer())
        metered = Metered(inner, Budget(max_tokens=100_000))
        metered.ask(PROMPT)

        stopped = metered.going_offline()

        assert isinstance(stopped.ask(PROMPT), Declined)
        assert inner.calls == 1

    def test_going_offline_before_any_call_still_refuses(self) -> None:
        """The case my first version got wrong.

        It expressed "stop now" as a call ceiling equal to the number already
        spent. Before the first call that is zero, and a ceiling of zero means
        unlimited -- so switching the model off before using it turned every
        limit off instead. Stopping is not a quantity.
        """
        inner = CountingProvider(answer())
        metered = Metered(inner, Budget(max_calls=5))

        stopped = metered.going_offline()

        assert isinstance(stopped.ask(PROMPT), Declined)
        assert inner.calls == 0

    def test_going_offline_keeps_what_was_spent(self) -> None:
        metered = Metered(CountingProvider(answer(tokens=100)), Budget(max_tokens=100_000))
        metered.ask(PROMPT)

        stopped = metered.going_offline()

        assert stopped.budget.spent_tokens == 100

    def test_the_reason_says_it_was_switched_off(self) -> None:
        stopped = Metered(CountingProvider(answer()), Budget()).going_offline()

        reply = stopped.ask(PROMPT)

        assert isinstance(reply, Declined)
        assert "switched off" in reply.reason


class TestRateLimiting:
    """Injected clock and sleeper, because a test that really sleeps is a test
    somebody eventually deletes."""

    def test_the_first_call_does_not_wait(self) -> None:
        limit = RateLimit(minimum_interval=1.0, monotonic=lambda: 100.0, sleeper=lambda _: None)

        assert limit.wait() == 0.0

    def test_a_close_second_call_waits_the_remainder(self) -> None:
        clock = iter([100.0, 100.25])
        slept: list[float] = []
        limit = RateLimit(minimum_interval=1.0, monotonic=lambda: next(clock), sleeper=slept.append)
        limit.wait()

        delay = limit.wait()

        assert delay == pytest.approx(0.75)
        assert slept == [pytest.approx(0.75)]

    def test_a_late_second_call_does_not_wait(self) -> None:
        clock = iter([100.0, 105.0])
        slept: list[float] = []
        limit = RateLimit(minimum_interval=1.0, monotonic=lambda: next(clock), sleeper=slept.append)
        limit.wait()

        assert limit.wait() == 0.0
        assert slept == []

    def test_no_interval_means_no_waiting(self) -> None:
        slept: list[float] = []
        limit = RateLimit(minimum_interval=0.0, sleeper=slept.append)

        limit.wait()
        limit.wait()

        assert slept == []

    def test_an_allowed_call_does_wait(self) -> None:
        """The contrast to the test below, and the gap it left.

        A test that only checks a refused call does not sleep is satisfied by a
        provider that never sleeps at all -- which is how deleting the wait
        entirely survived a first round of mutation. This asserts the limiter
        is reached on the path where it is supposed to bite.
        """
        clock = iter([100.0, 100.1])
        slept: list[float] = []
        metered = Metered(
            CountingProvider(answer()),
            Budget(),
            RateLimit(minimum_interval=1.0, monotonic=lambda: next(clock), sleeper=slept.append),
        )

        metered.ask(PROMPT)
        metered.ask(PROMPT)

        assert slept == [pytest.approx(0.9)]

    def test_a_refused_call_is_not_rate_limited(self) -> None:
        """Sleeping before declining wastes a user's afternoon for nothing."""
        slept: list[float] = []
        metered = Metered(
            CountingProvider(answer()),
            Budget(max_calls=0, max_tokens=1),
            RateLimit(minimum_interval=60.0, sleeper=slept.append),
        )

        metered.ask(PROMPT)

        assert slept == []


class TestNaming:
    def test_the_name_says_it_is_metered(self) -> None:
        assert Metered(NullProvider(), Budget()).name == "metered:null"

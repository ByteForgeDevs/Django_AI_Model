"""Spending limits, and what happens at the limit.

The interesting question is not how to count tokens. It is what a run does when
the count is reached, and the answer this module enforces is: exactly what it
would have done with no model at all. Running out of budget degrades djaudit to
its deterministic behaviour, which is the behaviour CI already exercises on
every commit through ``NullProvider``. There is no separate low-budget mode to
get wrong.

That makes the budget safe to set aggressively. A user who caps a run at a
thousand tokens gets a complete, correct finding list with commentary on the
first few findings, not a truncated audit.

The estimator is deliberately crude and deliberately pessimistic. A budget
enforced against a guess that runs low is a budget that is exceeded, so the
guess rounds against us, and the real cost from the provider replaces the
estimate as soon as one is known.

One limitation, stated rather than papered over: the check runs *before* a
call, against the prompt, and a response's size cannot be known in advance --
a schema with a free-text field has no upper bound. So a ceiling can be
overshot by at most one response. It is a ceiling on what a run will start, not
a guarantee about what it finishes. Bounding it properly would mean either
truncating replies, which corrupts them, or refusing to start well below the
limit, which wastes most of it.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from djaudit.llm.provider import Answer, Declined, Prompt, Provider, Reply, Usage

# Four characters per token is the usual English rule of thumb, and code is
# denser than English -- more punctuation, more short identifiers. Three is the
# pessimistic end of the published range, which is the end to be on when the
# number is used to decide whether there is room for one more call.
CHARACTERS_PER_TOKEN = 3


def estimate_tokens(text: str) -> int:
    """A deliberate over-estimate of what ``text`` will cost."""
    return -(-len(text) // CHARACTERS_PER_TOKEN)


def estimate_prompt(prompt: Prompt) -> int:
    # The schema is sent too, and on a small prompt it is not a rounding error.
    schema_size = sum(
        len(f.name) + len(f.description) + sum(len(c) for c in f.choices) + 24
        for f in prompt.schema.fields
    )
    return estimate_tokens(prompt.system) + estimate_tokens(prompt.user) + schema_size // 4


class BudgetExhaustedError(RuntimeError):
    """Raised only by ``Budget.charge``; callers see a ``Declined`` instead."""


@dataclass
class Budget:
    """A ceiling on tokens and calls, and a record of what was actually spent.

    Mutable, unlike almost everything else in this package, because it is the
    one thing that must accumulate across a run. It is not shared across
    threads.
    """

    max_tokens: int = 0
    max_calls: int = 0
    spent_tokens: int = 0
    spent_calls: int = 0
    declined_calls: int = 0
    # Set when a run gives up on the model entirely. A separate flag rather
    # than a zeroed ceiling: my first version expressed "stop now" by setting
    # max_calls to the number already spent, which is 0 before the first call,
    # and a max_calls of 0 means unlimited. Stopping is not a quantity.
    stopped: bool = False

    @property
    def unlimited(self) -> bool:
        return self.max_tokens <= 0 and self.max_calls <= 0

    def remaining_tokens(self) -> int | None:
        if self.max_tokens <= 0:
            return None
        return max(0, self.max_tokens - self.spent_tokens)

    def can_afford(self, estimate: int) -> str | None:
        """``None`` if there is room, else the reason there is not.

        A reason rather than a boolean, because "you asked for one call" and
        "you asked for a hundred thousand tokens" are different mistakes and
        the user should be told which one they made.
        """
        if self.stopped:
            return "the model was switched off for the rest of this run"
        if self.max_calls > 0 and self.spent_calls >= self.max_calls:
            return f"call budget spent ({self.spent_calls}/{self.max_calls} calls)"
        remaining = self.remaining_tokens()
        if remaining is not None and estimate > remaining:
            return (
                f"token budget spent ({self.spent_tokens}/{self.max_tokens} tokens, "
                f"next call needs about {estimate})"
            )
        return None

    def charge(self, usage: Usage) -> None:
        self.spent_tokens += usage.total
        self.spent_calls += 1

    def refuse(self) -> None:
        self.declined_calls += 1


@dataclass
class RateLimit:
    """A minimum spacing between calls, enforced by sleeping.

    Sleeping rather than queueing, because this is a batch tool with one job.
    ``sleeper`` is injected so the tests can assert on the delay that would
    have been taken without taking it -- a test that really sleeps is a test
    people delete.
    """

    minimum_interval: float = 0.0
    _last_call: float | None = None
    monotonic: Callable[[], float] = time.monotonic
    sleeper: Callable[[float], None] = time.sleep

    def wait(self) -> float:
        """Block if needed; return the delay taken, for the record."""
        if self.minimum_interval <= 0:
            return 0.0
        now = self.monotonic()
        if self._last_call is not None:
            elapsed = now - self._last_call
            if elapsed < self.minimum_interval:
                delay = self.minimum_interval - elapsed
                self.sleeper(delay)
                self._last_call = now + delay
                return delay
        self._last_call = now
        return 0.0


@dataclass
class Metered:
    """A provider that stops asking once the budget is gone.

    Wrapping rather than checking at each call site, for the same reason the
    cache is a provider: "was the budget respected here" should not be a
    question about call sites.
    """

    inner: Provider
    budget: Budget = field(default_factory=Budget)
    rate_limit: RateLimit = field(default_factory=RateLimit)

    @property
    def name(self) -> str:
        return f"metered:{self.inner.name}"

    def ask(self, prompt: Prompt) -> Reply:
        estimate = estimate_prompt(prompt)
        reason = self.budget.can_afford(estimate)
        if reason is not None:
            self.budget.refuse()
            # A refusal, not an exception. Every caller in this package already
            # handles Declined because NullProvider is the CI path, so
            # exhaustion travels a route that is tested on every commit.
            return Declined(reason)

        self.rate_limit.wait()
        reply = self.inner.ask(prompt)

        if isinstance(reply, Answer):
            # A cache hit cost nothing and is charged nothing; the cache
            # already zeroes its usage. A provider that reports no usage is
            # charged the estimate instead, because a call whose cost is
            # unknown must not be free -- that is how a budget is escaped.
            if reply.cached:
                return reply
            self.budget.charge(reply.usage if reply.usage.total else Usage(input_tokens=estimate))
        else:
            self.budget.refuse()
        return reply

    def going_offline(self, reason: str = "") -> Metered:
        """A copy that declines everything from here on, keeping what was spent.

        For the caller that meets an unreachable provider and decides one
        failure is enough. The spend is carried over so a summary still reports
        what the run actually cost.
        """
        return replace(self, budget=replace(self.budget, stopped=True))

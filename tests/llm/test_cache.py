"""What the cache must key on, and what it must never store.

Two properties carry this module. The key has to cover every input that could
change the answer, which is asserted one input at a time rather than in bulk --
a test that changes everything at once passes even if the key covers only one
thing. And the stored entry has to be free of the credential and of the source
code, which is checked by reading the bytes off disk rather than by inspecting
the object that was passed in.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from djaudit.llm.cache import CACHE_FORMAT, Cache, Cached, key_for
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
    fields=(
        Field("verdict", FieldKind.STRING, "is it real", choices=("real", "noise")),
        Field("why", FieldKind.STRING, "one sentence"),
    )
)

PROMPT = Prompt(
    version="triage/1",
    system="You review static analysis findings.",
    user="Is DJS-002 at settings.py:14 a real defect?",
    schema=SCHEMA,
)


@dataclass
class RecordingProvider:
    """Answers a fixed reply and counts how often it was actually asked.

    The count is the point. "The cache returned the right value" is satisfied by
    a cache that does nothing at all and calls through every time.
    """

    reply: Reply
    calls: list[Prompt] = field(default_factory=list)
    name: str = "recording:model-x"

    def ask(self, prompt: Prompt) -> Reply:
        self.calls.append(prompt)
        return self.reply


ANSWER = Answer(
    content={"verdict": "real", "why": "SECRET_KEY is a literal"},
    model="model-x",
    usage=Usage(input_tokens=900, output_tokens=40),
)


class TestTheKeyCoversEveryInput:
    """One field at a time, because changing several at once proves nothing."""

    def test_the_same_question_gives_the_same_key(self) -> None:
        assert key_for(PROMPT, model="m", fingerprint="f") == key_for(
            PROMPT, model="m", fingerprint="f"
        )

    def test_a_different_finding_is_a_different_key(self) -> None:
        assert key_for(PROMPT, model="m", fingerprint="f1") != key_for(
            PROMPT, model="m", fingerprint="f2"
        )

    def test_a_different_model_is_a_different_key(self) -> None:
        assert key_for(PROMPT, model="m1", fingerprint="f") != key_for(
            PROMPT, model="m2", fingerprint="f"
        )

    def test_a_bumped_prompt_version_is_a_different_key(self) -> None:
        later = Prompt(version="triage/2", system=PROMPT.system, user=PROMPT.user, schema=SCHEMA)
        assert key_for(PROMPT, model="m", fingerprint="f") != key_for(
            later, model="m", fingerprint="f"
        )

    def test_reworded_user_text_is_a_different_key(self) -> None:
        """The wording, not only the version.

        A version bump is something a person has to remember. This is the
        backstop for when they do not.
        """
        edited = Prompt(
            version=PROMPT.version,
            system=PROMPT.system,
            user=PROMPT.user + " Answer carefully.",
            schema=SCHEMA,
        )
        assert key_for(PROMPT, model="m", fingerprint="f") != key_for(
            edited, model="m", fingerprint="f"
        )

    def test_a_reworded_system_prompt_is_a_different_key(self) -> None:
        edited = Prompt(
            version=PROMPT.version,
            system="You are terse.",
            user=PROMPT.user,
            schema=SCHEMA,
        )
        assert key_for(PROMPT, model="m", fingerprint="f") != key_for(
            edited, model="m", fingerprint="f"
        )

    def test_an_added_field_is_a_different_key(self) -> None:
        wider = Prompt(
            version=PROMPT.version,
            system=PROMPT.system,
            user=PROMPT.user,
            schema=ResponseSchema(
                fields=(*SCHEMA.fields, Field("rank", FieldKind.INTEGER, "1-10"))
            ),
        )
        assert key_for(PROMPT, model="m", fingerprint="f") != key_for(
            wider, model="m", fingerprint="f"
        )

    def test_a_widened_choice_set_is_a_different_key(self) -> None:
        """A closed set is part of the question, not a formatting detail."""
        widened = Prompt(
            version=PROMPT.version,
            system=PROMPT.system,
            user=PROMPT.user,
            schema=ResponseSchema(
                fields=(
                    Field(
                        "verdict",
                        FieldKind.STRING,
                        "is it real",
                        choices=("real", "noise", "unsure"),
                    ),
                    Field("why", FieldKind.STRING, "one sentence"),
                )
            ),
        )
        assert key_for(PROMPT, model="m", fingerprint="f") != key_for(
            widened, model="m", fingerprint="f"
        )

    def test_reordering_fields_is_a_different_key(self) -> None:
        """Because it is a different request, which I had to read to learn.

        My first version of the digest deliberately normalised field order
        away, reasoning that the same fields in a different order ask the same
        question. ``ResponseSchema.as_json_schema`` builds ``properties`` in
        field order and ``enum`` in choice order, so both orderings are sent to
        the provider and either can move an answer. The key now hashes the
        rendered document, which needs no judgement about which parts matter.
        """
        reordered = Prompt(
            version=PROMPT.version,
            system=PROMPT.system,
            user=PROMPT.user,
            schema=ResponseSchema(fields=(SCHEMA.fields[1], SCHEMA.fields[0])),
        )
        assert key_for(PROMPT, model="m", fingerprint="f") != key_for(
            reordered, model="m", fingerprint="f"
        )

    def test_reordering_two_optional_fields_is_still_a_different_key(self) -> None:
        """The same property, with the accidental backstop removed.

        The test above passes even against a digest that sorts field names,
        because ``required`` is a list and a list's order survives sorting a
        document's keys. That is a fallback producing the right answer for the
        wrong reason. With both fields optional the ``required`` list is empty
        either way, so only a digest that genuinely preserves field order can
        tell these two requests apart -- and they are two requests, since
        ``properties`` reaches the provider in the order it was built.
        """
        first = Field("a", FieldKind.STRING, "first", required=False)
        second = Field("b", FieldKind.STRING, "second", required=False)
        one = Prompt(
            version="v1", system="s", user="u", schema=ResponseSchema(fields=(first, second))
        )
        other = Prompt(
            version="v1", system="s", user="u", schema=ResponseSchema(fields=(second, first))
        )

        assert one.schema.as_json_schema()["required"] == []
        assert key_for(one, model="m", fingerprint="f") != key_for(
            other, model="m", fingerprint="f"
        )

    def test_an_equal_schema_built_twice_is_the_same_key(self) -> None:
        """The contrast, without which all of the above is satisfied by a key
        that changes on everything and caches nothing.

        Two separately constructed but equal schemas must agree, which also
        rules out a digest that keys on object identity.
        """
        rebuilt = Prompt(
            version="triage/1",
            system="You review static analysis findings.",
            user="Is DJS-002 at settings.py:14 a real defect?",
            schema=ResponseSchema(
                fields=(
                    Field("verdict", FieldKind.STRING, "is it real", choices=("real", "noise")),
                    Field("why", FieldKind.STRING, "one sentence"),
                )
            ),
        )
        assert rebuilt is not PROMPT
        assert key_for(PROMPT, model="m", fingerprint="f") == key_for(
            rebuilt, model="m", fingerprint="f"
        )

    def test_a_reworded_field_description_is_a_different_key(self) -> None:
        """Descriptions are sent. They are instructions, not comments."""
        redescribed = Prompt(
            version=PROMPT.version,
            system=PROMPT.system,
            user=PROMPT.user,
            schema=ResponseSchema(
                fields=(
                    Field(
                        "verdict",
                        FieldKind.STRING,
                        "is it exploitable in production",
                        choices=("real", "noise"),
                    ),
                    SCHEMA.fields[1],
                )
            ),
        )
        assert key_for(PROMPT, model="m", fingerprint="f") != key_for(
            redescribed, model="m", fingerprint="f"
        )


class TestItActuallyCaches:
    def test_a_second_ask_does_not_reach_the_provider(self, tmp_path: Path) -> None:
        inner = RecordingProvider(ANSWER)
        cached = Cached(inner, Cache(tmp_path)).for_finding("abc123")

        first = cached.ask(PROMPT)
        second = cached.ask(PROMPT)

        assert len(inner.calls) == 1
        assert isinstance(first, Answer)
        assert isinstance(second, Answer)
        assert second.content == first.content

    def test_a_different_finding_does_reach_the_provider(self, tmp_path: Path) -> None:
        inner = RecordingProvider(ANSWER)
        cache = Cache(tmp_path)
        Cached(inner, cache).for_finding("abc123").ask(PROMPT)
        Cached(inner, cache).for_finding("def456").ask(PROMPT)

        assert len(inner.calls) == 2

    def test_a_hit_is_labelled_as_one(self, tmp_path: Path) -> None:
        """A reader has to be able to tell a fresh answer from a stored one."""
        inner = RecordingProvider(ANSWER)
        cached = Cached(inner, Cache(tmp_path)).for_finding("abc123")

        fresh = cached.ask(PROMPT)
        stored = cached.ask(PROMPT)

        assert isinstance(fresh, Answer)
        assert isinstance(stored, Answer)
        assert fresh.cached is False
        assert stored.cached is True

    def test_a_hit_costs_nothing(self, tmp_path: Path) -> None:
        """Otherwise one call is charged against the budget twice."""
        inner = RecordingProvider(ANSWER)
        cached = Cached(inner, Cache(tmp_path)).for_finding("abc123")
        cached.ask(PROMPT)

        stored = cached.ask(PROMPT)

        assert isinstance(stored, Answer)
        assert stored.usage.total == 0

    def test_a_disabled_cache_stores_nothing(self, tmp_path: Path) -> None:
        inner = RecordingProvider(ANSWER)
        cached = Cached(inner, Cache(tmp_path, enabled=False)).for_finding("abc123")

        cached.ask(PROMPT)
        cached.ask(PROMPT)

        assert len(inner.calls) == 2
        assert list(tmp_path.rglob("*.json")) == []


class TestWhatIsNeverCached:
    def test_a_refusal_is_not_stored(self, tmp_path: Path) -> None:
        """Declining is about the machine, not the question.

        No key, no network, budget spent: all transient. Caching a refusal
        makes a temporary condition permanent, and the user who exports their
        key and re-runs gets the same silence.
        """
        inner = RecordingProvider(Declined("no credentials"))
        cached = Cached(inner, Cache(tmp_path)).for_finding("abc123")

        cached.ask(PROMPT)
        cached.ask(PROMPT)

        assert len(inner.calls) == 2
        assert list(tmp_path.rglob("*.json")) == []

    def test_the_null_provider_leaves_no_entries(self, tmp_path: Path) -> None:
        cached = Cached(NullProvider(), Cache(tmp_path)).for_finding("abc123")

        assert isinstance(cached.ask(PROMPT), Declined)
        assert list(tmp_path.rglob("*.json")) == []


class TestNothingSecretReachesDisk:
    """Read the bytes, not the object.

    A cache directory ends up in tarballs, in CI artifacts and in `find . -type
    f` output. Asserting on the payload we constructed would only prove we
    built it correctly.
    """

    def test_no_credential_is_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        secret = "sk-livekeyaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        monkeypatch.setenv("DJAUDIT_CACHE_TEST_KEY", secret)
        inner = RecordingProvider(ANSWER)

        Cached(inner, Cache(tmp_path)).for_finding("abc123").ask(PROMPT)

        written = list(tmp_path.rglob("*.json"))
        assert written, "the test proves nothing if nothing was written"
        for path in written:
            assert secret not in path.read_text(encoding="utf-8")
            assert secret not in str(path)

    def test_the_key_is_a_hash_not_the_question(self, tmp_path: Path) -> None:
        """Filenames leak. A path is visible to anyone who can list a directory."""
        inner = RecordingProvider(ANSWER)
        Cached(inner, Cache(tmp_path)).for_finding("abc123").ask(PROMPT)

        names = [p.name for p in tmp_path.rglob("*.json")]
        assert names
        for name in names:
            assert "settings.py" not in name
            assert "DJS-002" not in name
            assert "abc123" not in name

    def test_the_prompt_text_is_not_stored(self, tmp_path: Path) -> None:
        """The question quotes source. The answer does not need to."""
        inner = RecordingProvider(ANSWER)
        Cached(inner, Cache(tmp_path)).for_finding("abc123").ask(PROMPT)

        for path in tmp_path.rglob("*.json"):
            body = path.read_text(encoding="utf-8")
            assert PROMPT.user not in body
            assert PROMPT.system not in body


class TestADamagedEntry:
    def test_truncated_json_is_a_miss_not_a_crash(self, tmp_path: Path) -> None:
        inner = RecordingProvider(ANSWER)
        cached = Cached(inner, Cache(tmp_path)).for_finding("abc123")
        cached.ask(PROMPT)
        entry = next(iter(tmp_path.rglob("*.json")))
        entry.write_text('{"format": 1, "conte', encoding="utf-8")

        again = cached.ask(PROMPT)

        assert isinstance(again, Answer)
        assert len(inner.calls) == 2

    def test_an_older_format_is_ignored(self, tmp_path: Path) -> None:
        """The format number is the escape hatch for changing what we store."""
        inner = RecordingProvider(ANSWER)
        cached = Cached(inner, Cache(tmp_path)).for_finding("abc123")
        cached.ask(PROMPT)
        entry = next(iter(tmp_path.rglob("*.json")))
        stored = json.loads(entry.read_text(encoding="utf-8"))
        stored["format"] = CACHE_FORMAT - 1
        entry.write_text(json.dumps(stored), encoding="utf-8")

        cached.ask(PROMPT)

        assert len(inner.calls) == 2

    def test_a_content_that_is_not_an_object_is_ignored(self, tmp_path: Path) -> None:
        inner = RecordingProvider(ANSWER)
        cached = Cached(inner, Cache(tmp_path)).for_finding("abc123")
        cached.ask(PROMPT)
        entry = next(iter(tmp_path.rglob("*.json")))
        entry.write_text(
            json.dumps({"format": CACHE_FORMAT, "content": ["real"], "model": "m"}),
            encoding="utf-8",
        )

        cached.ask(PROMPT)

        assert len(inner.calls) == 2

    def test_an_empty_directory_is_a_miss(self, tmp_path: Path) -> None:
        assert Cache(tmp_path / "never-created").get("a" * 64) is None


class TestWritesAreAtomic:
    def test_no_temporary_file_is_left_behind(self, tmp_path: Path) -> None:
        inner = RecordingProvider(ANSWER)
        Cached(inner, Cache(tmp_path)).for_finding("abc123").ask(PROMPT)

        assert list(tmp_path.rglob("*.tmp")) == []

    def test_a_failed_write_leaves_no_entry(self, tmp_path: Path) -> None:
        """Better no entry than half of one, since half is indistinguishable
        from a complete answer to a shorter question."""
        cache = Cache(tmp_path)
        unserialisable = Answer(content={"verdict": object()}, model="m")

        with pytest.raises(TypeError):
            cache.put("a" * 64, unserialisable)

        assert list(tmp_path.rglob("*.tmp")) == []
        assert list(tmp_path.rglob("*.json")) == []

    def test_a_crash_midway_through_a_write_leaves_no_entry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reason for the rename, tested at the moment it matters.

        Serialisation failures are caught by the test above without any of this
        machinery, because nothing has been written when they happen. This one
        fails *after* bytes are on disk, which is the case a direct
        ``write_text`` cannot survive: it would leave a truncated ``.json``
        where a complete one is expected. Writing elsewhere and renaming means
        the entry either exists in full or does not exist.
        """

        def die_midway(payload: object, stream: Any, **kwargs: object) -> None:
            stream.write('{"format": 1, "cont')
            raise OSError("disk full")

        monkeypatch.setattr("djaudit.llm.cache.json.dump", die_midway)
        cache = Cache(tmp_path)

        with pytest.raises(OSError, match="disk full"):
            cache.put("a" * 64, ANSWER)

        assert list(tmp_path.rglob("*.json")) == []
        assert list(tmp_path.rglob("*.tmp")) == []

    def test_a_rewrite_replaces_rather_than_appends(self, tmp_path: Path) -> None:
        cache = Cache(tmp_path)
        cache.put("a" * 64, ANSWER)
        cache.put("a" * 64, Answer(content={"verdict": "noise", "why": "test"}, model="m"))

        hit = cache.get("a" * 64)
        assert hit is not None
        assert hit.content["verdict"] == "noise"


class TestHousekeeping:
    def test_entries_are_sharded(self, tmp_path: Path) -> None:
        """One flat directory of a hundred thousand files is a mistake that is
        only visible at the scale where it hurts."""
        cache = Cache(tmp_path)
        cache.put("ab" + "c" * 62, ANSWER)

        assert (tmp_path / "ab").is_dir()

    def test_clear_removes_entries_and_counts_them(self, tmp_path: Path) -> None:
        cache = Cache(tmp_path)
        cache.put("a" * 64, ANSWER)
        cache.put("b" * 64, ANSWER)

        assert cache.clear() == 2
        assert cache.get("a" * 64) is None

    def test_the_name_says_it_is_cached(self, tmp_path: Path) -> None:
        assert Cached(NullProvider(), Cache(tmp_path)).name == "cached:null"

    def test_the_directory_is_created_on_demand(self, tmp_path: Path) -> None:
        cache = Cache(tmp_path / "deep" / "nested")
        cache.put("a" * 64, ANSWER)

        assert cache.get("a" * 64) is not None

    def test_entries_are_stable_across_processes(self, tmp_path: Path) -> None:
        """The key must not depend on hash randomisation.

        Python salts str hashing per process. A key built from `hash()` or from
        iteration over a set would be stable within one run and useless across
        two, which is exactly the failure a test in a single process cannot
        see -- so this asserts the digest against a subprocess with a different
        seed rather than against itself.
        """
        import subprocess
        import sys
        import textwrap

        here = key_for(PROMPT, model="m", fingerprint="f")
        script = textwrap.dedent(
            """
            from djaudit.llm.cache import key_for
            from djaudit.llm.provider import Field, FieldKind, Prompt, ResponseSchema
            schema = ResponseSchema(fields=(
                Field("verdict", FieldKind.STRING, "is it real", choices=("real", "noise")),
                Field("why", FieldKind.STRING, "one sentence"),
            ))
            print(key_for(Prompt(
                version="triage/1",
                system="You review static analysis findings.",
                user="Is DJS-002 at settings.py:14 a real defect?",
                schema=schema,
            ), model="m", fingerprint="f"))
            """
        )
        environment = {**os.environ, "PYTHONHASHSEED": "12345"}
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=True,
            env=environment,
        )

        assert result.stdout.strip() == here

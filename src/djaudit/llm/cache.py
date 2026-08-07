"""Answers on disk, keyed by everything that could change them.

A cache makes a model layer affordable and, more usefully here, reproducible: a
triage run that is re-run should not quietly reorder itself because a sampler
rolled differently. That only holds if the key covers every input. A key that
omits one is worse than no cache, because it serves a confident answer to a
question nobody asked.

So the key is the finding fingerprint, the prompt version, the rendered prompt
text, the response schema and the model id, hashed together. Change any of
them -- reword a prompt, add a field, switch models, or point at a different
finding -- and the entry does not match. The prompt text is included as well as
its version because a version people have to remember to bump is a version that
does not get bumped.

Nothing here is secret and nothing here should be: the stored value is a reply
that was already validated against a caller's schema, and the key is a hash.
The credential is not part of either, which is checked rather than assumed,
because a cache directory is the sort of thing that ends up in a tarball.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

from djaudit.llm.provider import Answer, Prompt, Provider, Reply, ResponseSchema, Usage

CACHE_FORMAT = 1


def _schema_digest(schema: ResponseSchema) -> str:
    """The identity of the schema *as it will be sent*.

    An earlier version of this hashed a set of field descriptors with the
    ordering normalised away, on the assumption that declaring the same fields
    in a different order asks the same question. Reading ``as_json_schema``
    says otherwise: ``properties`` is built in field order and ``enum`` in
    choice order, so both orderings reach the provider and both can move an
    answer. Hashing the rendered document instead makes the key cover exactly
    the bytes the model sees, with no judgement about which parts matter.
    """
    rendered = json.dumps(schema.as_json_schema(), separators=(",", ":"))
    return hashlib.sha256(rendered.encode()).hexdigest()[:16]


def key_for(prompt: Prompt, *, model: str, fingerprint: str) -> str:
    """Everything that could change the answer, and nothing that could not."""
    material = "\x00".join(
        (
            str(CACHE_FORMAT),
            fingerprint,
            prompt.version,
            model,
            _schema_digest(prompt.schema),
            prompt.system,
            prompt.user,
        )
    )
    return hashlib.sha256(material.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Cache:
    """One JSON file per entry, under a directory the caller owns.

    A directory of small files rather than one index, so two runs in parallel
    cannot corrupt each other's work and a stale entry can be deleted with
    ``rm``. Writes go through a temporary file and an atomic rename for the
    same reason.
    """

    directory: Path
    enabled: bool = True

    def _path(self, key: str) -> Path:
        # Two characters of prefix, so a large corpus does not put a hundred
        # thousand files in one directory.
        return self.directory / key[:2] / f"{key}.json"

    def get(self, key: str) -> Answer | None:
        if not self.enabled:
            return None
        path = self._path(key)
        if not path.is_file():
            return None
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A truncated entry is a cache miss, not a crash. The next write
            # replaces it.
            return None
        if stored.get("format") != CACHE_FORMAT:
            return None
        content = stored.get("content")
        if not isinstance(content, dict):
            return None
        return Answer(
            content=content,
            model=str(stored.get("model", "")),
            # A cached answer cost nothing this run. Recording the original
            # cost here would let a budget be spent twice on one call.
            usage=Usage(),
            cached=True,
        )

    def put(self, key: str, answer: Answer) -> None:
        if not self.enabled:
            return
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "format": CACHE_FORMAT,
            "content": answer.content,
            "model": answer.model,
        }
        # Atomic, so a reader never sees half a file and an interrupted run
        # leaves no entry rather than a corrupt one.
        handle, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, sort_keys=True)
            Path(temporary).replace(path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    def clear(self) -> int:
        removed = 0
        for path in self.directory.rglob("*.json"):
            path.unlink()
            removed += 1
        return removed


@dataclass(frozen=True, slots=True)
class Cached:
    """A provider wrapped in a cache.

    Written as a provider rather than as a helper the callers remember to use,
    so that "did this go through the cache" is not a question about call sites.
    """

    inner: Provider
    cache: Cache
    fingerprint: str = ""

    @property
    def name(self) -> str:
        return f"cached:{self.inner.name}"

    def for_finding(self, fingerprint: str) -> Cached:
        return replace(self, fingerprint=fingerprint)

    def ask(self, prompt: Prompt) -> Reply:
        # ``name`` is the model's identity, which is why a provider's name has
        # to include its model and not just its vendor. Two models behind one
        # name would share cache entries and answer for each other.
        key = key_for(prompt, model=self.inner.name, fingerprint=self.fingerprint)
        hit = self.cache.get(key)
        if hit is not None:
            return hit
        reply = self.inner.ask(prompt)
        if isinstance(reply, Answer):
            self.cache.put(key, reply)
        # A refusal is never cached. Declining is usually about the state of
        # the machine -- no key, no network, budget spent -- and caching it
        # would make a transient condition permanent.
        return reply

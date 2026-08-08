"""Byte-range edits that prove they touched only what they meant to.

A tool that rewrites someone's source has one obligation above being useful: it
must not damage anything it did not intend to change. Whole-file approaches
fail that by construction. `ast.unparse` round-trips a module into valid Python
that has lost every comment, every blank line and every formatting choice the
author made -- the diff is the whole file, and no reviewer can read it. So the
unit here is not a file, it is a **range**: a start and end offset, and the text
to put between them. Everything outside the range is copied through untouched,
and `apply` proves that rather than assuming it.

**Why there is no CST dependency.** The plan called for `libcst`, so the range
approach was measured against the alternative before that dependency was taken
on. Across the three benchmark targets and the fixtures -- 3,159 files, 86,783
assignment values -- a range derived from `ast` position data was sliced out and
re-parsed:

| result | count | share |
|---|---|---|
| slice re-parses to an identical tree | 86,601 | **99.790%** |
| slice does not | 182 | 0.210% |

Every one of the 182 is the same shape: a value wrapped in parentheses that
`ast` excludes from its own range, so the slice is a fragment that only parses
inside brackets::

    QUERY = (Q(ssid__icontains=value) |
             Q(description__icontains=value))

The range covers the `|` expression but not the parens, and implicit line
joining does not survive the extraction. That is worth knowing precisely,
because it is **detectable from inside**: parse the slice and compare it to the
node it came from. `verified_span` does exactly that and returns `None` when it
fails, so the 0.21% is refused rather than corrupted. A ranged edit is therefore
not "99.79% safe" -- it is safe, and available 99.79% of the time. `libcst`
would have bought the remaining 0.21% at the cost of a dependency an order of
magnitude larger than everything else we ship, and would not have supplied the
round-trip proof, which is the part that makes an edit publishable. It stays an
optional extra for a future rewrite that genuinely needs a CST; nothing imports
it today.

**`ast` columns are UTF-8 byte offsets, not character offsets.** A line with a
non-ASCII character before the target shifts every column after it. Converting
with `len(line[:col])` is wrong and silently produces an off-by-n range that
still parses -- the worst kind of failure, because it edits the wrong bytes and
raises nothing. `offset_of` encodes the line and decodes the prefix instead.
"""

from __future__ import annotations

import ast
import difflib
from bisect import bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

# Enough context to see what an edit sits inside without printing the file.
DIFF_CONTEXT = 3


class EditError(Exception):
    """An edit could not be made safely, so it was not made at all."""


@dataclass(frozen=True)
class Edit:
    """A replacement of `source[start:end]`, and the reason for it.

    Offsets are into the decoded text, not the encoded bytes; `offset_of`
    handles the conversion from what `ast` reports.
    """

    start: int
    end: int
    replacement: str
    why: str

    def __post_init__(self) -> None:
        if self.start < 0 or self.end < self.start:
            raise EditError(f"an edit cannot span {self.start}..{self.end}")

    @property
    def is_insertion(self) -> bool:
        return self.start == self.end

    def overlaps(self, other: Edit) -> bool:
        """Whether two edits contend for the same text.

        Two insertions at one point overlap: their order would decide the
        result, and nothing here defines that order.
        """
        if self.is_insertion and other.is_insertion:
            return self.start == other.start
        return self.start < other.end and other.start < self.end


def line_starts(source: str) -> list[int]:
    """Offset of the first character of each line, 0-indexed."""
    starts = [0]
    for line in source.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))
    return starts


def offset_of(source: str, line: int, column: int, starts: Sequence[int] | None = None) -> int:
    """Convert an `ast` position to a character offset.

    `line` is 1-based and `column` is a UTF-8 **byte** offset within that line,
    which is what every `ast` node carries.
    """
    starts = line_starts(source) if starts is None else starts
    if not 1 <= line < len(starts):
        raise EditError(f"line {line} is not in a file of {len(starts) - 1} lines")
    text = source[starts[line - 1] : starts[line]]
    prefix = text.encode("utf-8")[:column]
    return starts[line - 1] + len(prefix.decode("utf-8", errors="ignore"))


def span(source: str, node: ast.AST) -> tuple[int, int]:
    """The character range `node` occupies in `source`.

    Raises rather than guessing when the node carries no end position, which
    happens for synthesised nodes that were never parsed from this text.
    """
    start_line = getattr(node, "lineno", None)
    end_line = getattr(node, "end_lineno", None)
    start_col = getattr(node, "col_offset", None)
    end_col = getattr(node, "end_col_offset", None)
    if start_line is None or end_line is None or start_col is None or end_col is None:
        raise EditError(f"{type(node).__name__} carries no position; it was not parsed from source")
    starts = line_starts(source)
    return offset_of(source, start_line, start_col, starts), offset_of(
        source, end_line, end_col, starts
    )


def verified_span(source: str, node: ast.expr) -> tuple[int, int] | None:
    """`span`, but only when the slice provably means the same thing.

    Returns `None` for the 0.21% measured in the module docstring -- chiefly
    parenthesised multi-line expressions, whose range excludes the brackets that
    hold them together. Refusing is the point: a caller that cannot get a
    verified span must not fall back to an unverified one.
    """
    try:
        start, end = span(source, node)
    except EditError:
        return None
    try:
        extracted = ast.parse(source[start:end], mode="eval").body
    except (SyntaxError, ValueError):
        return None
    return (start, end) if ast.dump(extracted) == ast.dump(node) else None


def replace_value(source: str, node: ast.expr, new_source: str, why: str) -> Edit:
    """An edit swapping one expression for another.

    `new_source` must itself parse as an expression: a fix that writes a syntax
    error is worse than a fix that refuses.
    """
    try:
        ast.parse(new_source, mode="eval")
    except SyntaxError as exc:
        raise EditError(f"replacement {new_source!r} is not an expression: {exc}") from exc
    located = verified_span(source, node)
    if located is None:
        raise EditError(
            "this expression cannot be located exactly -- it is most likely wrapped in "
            "parentheses that its own range excludes, so no edit is offered here"
        )
    return Edit(start=located[0], end=located[1], replacement=new_source, why=why)


def apply(source: str, edits: Iterable[Edit]) -> str:
    """Apply non-overlapping edits, proving the untouched text is untouched.

    Overlapping edits raise. They could be ordered by some rule, but every such
    rule quietly decides which fix wins, and that decision belongs to whoever
    is reading the diff.
    """
    ordered = sorted(edits, key=lambda e: (e.start, e.end))
    for earlier, later in pairwise(ordered):
        if earlier.overlaps(later):
            raise EditError(
                f"edits {earlier.start}..{earlier.end} and {later.start}..{later.end} overlap; "
                "apply them in separate passes"
            )
    for e in ordered:
        if e.end > len(source):
            raise EditError(f"edit ends at {e.end}, past the end of a {len(source)}-character file")

    out: list[str] = []
    cursor = 0
    for e in ordered:
        out.append(source[cursor : e.start])
        out.append(e.replacement)
        cursor = e.end
    out.append(source[cursor:])
    result = "".join(out)

    _prove_only_the_ranges_moved(source, result, ordered)
    return result


def _prove_only_the_ranges_moved(source: str, result: str, ordered: Sequence[Edit]) -> None:
    """Re-derive the untouched text from both sides and require it to match.

    This is a check on `apply` itself rather than on its caller. It costs one
    string comparison and it is the difference between believing the edit was
    surgical and knowing it.
    """
    kept_before: list[str] = []
    kept_after: list[str] = []
    cursor = 0
    shift = 0
    for e in ordered:
        kept_before.append(source[cursor : e.start])
        kept_after.append(result[cursor + shift : e.start + shift])
        shift += len(e.replacement) - (e.end - e.start)
        cursor = e.end
    kept_before.append(source[cursor:])
    kept_after.append(result[cursor + shift :])
    if kept_before != kept_after:
        raise EditError("applying the edits changed text outside their ranges")


def touched_lines(source: str, edits: Iterable[Edit]) -> set[int]:
    """The 1-based lines an edit set reaches, for reporting."""
    starts = line_starts(source)
    lines: set[int] = set()
    for e in edits:
        first = bisect_right(starts, e.start)
        last = bisect_right(starts, max(e.start, e.end - 1))
        lines.update(range(first, last + 1))
    return lines


def diff(before: str, after: str, path: Path | str, context: int = DIFF_CONTEXT) -> str:
    """A unified diff, `git apply`-shaped.

    Empty when the two sides agree, so a caller can treat "no diff" as "nothing
    to propose" without a second comparison.
    """
    name = str(path)
    lines = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{name}",
        tofile=f"b/{name}",
        n=context,
    )
    text = "".join(lines)
    if text and not text.endswith("\n"):
        text += "\n"
    return text

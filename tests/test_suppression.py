"""Suppression must be explicit. Over-suppression hides security findings."""

from typing import ClassVar

from djaudit.suppression import is_suppressed, line_suppresses


class TestNoqa:
    def test_matching_code_suppresses(self):
        assert line_suppresses("DEBUG = True  # noqa: DJS-001", "DJS-001")

    def test_non_matching_code_does_not(self):
        assert not line_suppresses("DEBUG = True  # noqa: DJS-002", "DJS-001")

    def test_code_lists_are_honoured(self):
        assert line_suppresses("x  # noqa: DJP-004, DJS-001", "DJS-001")

    def test_bare_noqa_is_ignored(self):
        """Left behind for other linters; honouring it would hide real findings."""
        assert not line_suppresses("DEBUG = True  # noqa", "DJS-001")

    def test_other_tools_codes_are_ignored(self):
        assert not line_suppresses("from .base import *  # noqa: F401,F403", "DJS-001")

    def test_case_is_insensitive(self):
        assert line_suppresses("x  # NOQA: djs-001", "DJS-001")


class TestDjauditIgnore:
    def test_targeted_ignore_suppresses(self):
        assert line_suppresses("DEBUG = True  # djaudit: ignore[DJS-001]", "DJS-001")

    def test_targeted_ignore_is_scoped_to_its_codes(self):
        assert not line_suppresses("x  # djaudit: ignore[DJS-002]", "DJS-001")

    def test_bare_ignore_suppresses_everything(self):
        """Unlike bare noqa, this can only have been written for us."""
        assert line_suppresses("DEBUG = True  # djaudit: ignore", "DJS-001")

    def test_empty_bracket_list_suppresses_nothing(self):
        """``ignore[]`` is a typo, not a blanket suppression.

        The brackets announce "I am about to name rules". Naming none is a slip,
        and treating a slip as "suppress everything" is exactly the silent
        hiding of findings that bare ``# noqa`` is refused to prevent.
        """
        assert not line_suppresses("DEBUG = True  # djaudit: ignore[]", "DJS-001")
        assert not line_suppresses("DEBUG = True  # djaudit: ignore[   ]", "DJS-001")
        assert not line_suppresses("DEBUG = True  # djaudit: ignore[,]", "DJS-001")

    def test_bare_ignore_still_works_alongside_the_empty_list_rule(self):
        """Guard against a fix that over-corrects and breaks the bare form."""
        assert line_suppresses("x  # djaudit: ignore reason here", "DJS-001")

    def test_trailing_reason_is_allowed(self):
        assert line_suppresses(
            "DEBUG = True  # djaudit: ignore[DJS-001] staging box, PROJ-412", "DJS-001"
        )


class TestRanges:
    LINES: ClassVar[list[str]] = ["a = 1", "b = [", "    2,", "]  # noqa: DJS-001", "c = 3"]

    def test_comment_on_the_closing_line_of_a_statement_counts(self):
        assert is_suppressed(self.LINES, 2, 4, "DJS-001")

    def test_unrelated_lines_are_not_affected(self):
        assert not is_suppressed(self.LINES, 1, 1, "DJS-001")

    def test_out_of_range_is_safe(self):
        assert not is_suppressed(self.LINES, 900, 950, "DJS-001")
        assert not is_suppressed([], 1, 1, "DJS-001")

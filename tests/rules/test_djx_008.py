"""DJX-008 -- a regex lookup whose pattern means different things per engine.

The plan's original 5.2.7 was timezone handling in ``Trunc*``/``Extract``, and
measuring it withdrew it: twenty-eight comparisons across both engines --
``TruncDate``, ``TruncDay``, ``TruncWeek``, ``TruncQuarter``, ``TruncSecond``,
``ExtractHour``, ``ExtractWeek``, ``ExtractIsoYear``, ``ExtractIsoWeekDay``,
``ExtractSecond``, four timezones including a legacy alias and a half-hour
offset, DST gap and fold instants, and a per-database ``TIME_ZONE`` -- all
agreed exactly. Django registers Python helpers on the SQLite connection that
call ``zoneinfo`` and ``timezone.localtime``, so ``supports_timezones = False``
means "compensated in Python", not "answers differently".

This is the replacement, and it was measured the same way: twenty-seven regex
constructs run against the same eleven rows on a real SQLite and the live
Postgres. Twenty agree. Seven do not:

    \\bUSD\\b        sqlite 1   postgres 0        no error either side
    \\BSD           sqlite 1   postgres 0        no error either side
    [[:digit:]]+   sqlite 0   postgres 6        no error either side
    [[:alpha:]]+   sqlite 0   postgres 11       no error either side
    \\y555\\y        sqlite ERR postgres 1
    \\mUSD          sqlite ERR postgres 1
    (?P<w>abc)     sqlite 1   postgres ERR

``\\z``, ``\\Q...\\E`` and ``\\h`` fail on *both* and are deliberately not
reported: a pattern that is broken everywhere is not a portability defect.
"""

from __future__ import annotations

import pathlib

from djaudit import engine
from djaudit.models import Finding

MARKERS = (
    "SECRET_KEY = 'x'\nDEBUG = False\nALLOWED_HOSTS = ['example.com']\nINSTALLED_APPS = ['shop']\n"
)

LITE = "django.db.backends.sqlite3"
PG = "django.db.backends.postgresql"

DIVERGENT = (
    f"DATABASES = {{'default': {{'ENGINE': '{LITE}'}}}}\n"
    "import os\n"
    "if os.getenv('DB') == 'pg':\n"
    f"    DATABASES = {{'default': {{'ENGINE': '{PG}'}}}}\n"
)

POSTGRES_ONLY = f"DATABASES = {{'default': {{'ENGINE': '{PG}'}}}}\n"

MODELS = (
    "from django.db import models\n\n\n"
    "class Order(models.Model):\n"
    "    sku = models.CharField(max_length=32)\n"
)


def build(tmp_path: pathlib.Path, databases: str, queries: str) -> pathlib.Path:
    root = tmp_path / f"proj{len(list(tmp_path.iterdir()))}"
    (root / "config").mkdir(parents=True)
    (root / "shop").mkdir()
    (root / "manage.py").write_text(
        "import os\nos.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')\n"
    )
    (root / "config" / "__init__.py").write_text("")
    (root / "config" / "settings.py").write_text(MARKERS + databases)
    (root / "shop" / "__init__.py").write_text("")
    (root / "shop" / "models.py").write_text(MODELS)
    (root / "shop" / "queries.py").write_text(queries)
    return root


def findings(root: pathlib.Path) -> list[Finding]:
    result = engine.run(root)
    assert "DJX-008" not in result.rule_errors, result.rule_errors.get("DJX-008")
    return [f for f in result.findings if f.rule_id == "DJX-008"]


def query(pattern: str, lookup: str = "sku__regex") -> str:
    return (
        "from shop.models import Order\n\n\n"
        f"def search():\n    return Order.objects.filter({lookup}={pattern})\n"
    )


class TestTheSilentDivergences:
    """The dangerous half: the query succeeds on both and answers differently.

    Nothing raises, nothing logs, and the two engines return different rows --
    which is the whole reason this is a rule rather than a note in a wiki.
    """

    def test_a_python_word_boundary_is_reported(self, tmp_path: pathlib.Path) -> None:
        # Measured: `\bUSD\b` matched one row on SQLite and zero on Postgres.
        found = findings(build(tmp_path, DIVERGENT, query(r'r"\bUSD\b"')))
        assert len(found) == 1
        assert found[0].severity.value == "high"
        assert found[0].confidence.value == "certain"
        assert found[0].properties["constructs"] == "\\b"

    def test_it_explains_that_postgres_reads_it_as_a_backspace(
        self, tmp_path: pathlib.Path
    ) -> None:
        # The single most useful sentence in the finding. A developer who is
        # told only "not portable" will assume the pattern is unsupported;
        # they need to know it is silently a different pattern.
        message = findings(build(tmp_path, DIVERGENT, query(r'r"\bUSD\b"')))[0].message
        assert "literal backspace" in message
        assert "quietly matches different rows" in message

    def test_a_non_boundary_is_reported(self, tmp_path: pathlib.Path) -> None:
        found = findings(build(tmp_path, DIVERGENT, query(r'r"\BSD"')))
        assert found[0].properties["constructs"] == "\\B"
        # Asserting only the token left every word of this entry's prose free
        # to be wrong, which is most of what the reader actually gets.
        assert found[0].message == (
            "this regex is not portable: `\\B` matches a non-boundary under Python's "
            "`re` and is not a boundary construct at all under Postgres, so the "
            "pattern quietly matches different rows"
        )

    def test_a_posix_character_class_is_reported(self, tmp_path: pathlib.Path) -> None:
        # The mirror image: zero rows on SQLite, six on Postgres, because
        # Python's `re` reads `[[:digit:]]` as a nested set and warns rather
        # than raising.
        found = findings(build(tmp_path, DIVERGENT, query(r'r"[[:digit:]]+"')))
        assert found[0].properties["constructs"] == "[[:"
        assert "nested set" in found[0].message

    def test_all_three_are_marked_as_silent(self, tmp_path: pathlib.Path) -> None:
        for pattern in (r'r"\bUSD\b"', r'r"\BSD"', r'r"[[:alpha:]]+"'):
            found = findings(build(tmp_path, DIVERGENT, query(pattern)))
            assert found[0].properties["breaks"] == "silent", pattern


class TestTheLoudDivergences:
    def test_a_postgres_word_boundary_is_reported(self, tmp_path: pathlib.Path) -> None:
        # Measured: OperationalError on SQLite, one row on Postgres.
        found = findings(build(tmp_path, DIVERGENT, query(r'r"\y555\y"')))
        assert found[0].properties["constructs"] == "\\y"
        assert found[0].properties["breaks"] == "sqlite"
        assert "OperationalError on SQLite" in found[0].message

    def test_the_start_of_word_boundary_is_reported(self, tmp_path: pathlib.Path) -> None:
        found = findings(build(tmp_path, DIVERGENT, query(r'r"\mUSD"')))
        assert found[0].properties["constructs"] == "\\m"
        assert found[0].properties["breaks"] == "sqlite"
        assert found[0].message == (
            "this regex is not portable: `\\m` is Postgres' start-of-word boundary and "
            "is not valid in Python's `re`, so the query raises OperationalError on "
            "SQLite"
        )

    def test_the_end_of_word_boundary_is_reported(self, tmp_path: pathlib.Path) -> None:
        found = findings(build(tmp_path, DIVERGENT, query(r'r"USD\M"')))
        assert found[0].properties["constructs"] == "\\M"
        assert found[0].properties["breaks"] == "sqlite"
        assert found[0].message == (
            "this regex is not portable: `\\M` is Postgres' end-of-word boundary and "
            "is not valid in Python's `re`, so the query raises OperationalError on "
            "SQLite"
        )

    def test_a_python_named_group_breaks_the_other_engine(self, tmp_path: pathlib.Path) -> None:
        # The only construct measured to work on SQLite and fail on Postgres,
        # which is the direction that reaches production.
        found = findings(build(tmp_path, DIVERGENT, query(r'r"(?P<w>abc)"')))
        assert found[0].properties["constructs"] == "(?P<"
        assert found[0].properties["breaks"] == "postgres"
        assert "DataError on Postgres" in found[0].message


class TestWhatItDoesNotReport:
    """Twenty constructs were measured to agree, and silence on them is the
    difference between a rule and a blanket warning about regexes."""

    def test_the_portable_subset_is_not_reported(self, tmp_path: pathlib.Path) -> None:
        # Every one of these returned identical counts on both engines.
        portable = [
            r'r"\d+"',
            r'r"\w+-\w+"',
            r'r"a\s*c"',
            r'r"\S+"',
            r'r"abc(?=1)"',
            r'r"abc(?!9)"',
            r'r"(?<=abc)123"',
            r'r"^(a+)\1$"',
            r'r"(?i)abc"',
            r'r"(?s)line1.line2"',
            r'r"(?m)^line2"',
            r'r"\Aabc"',
            r'r"USD\Z"',
            r'r"a{2,}"',
        ]
        for pattern in portable:
            assert findings(build(tmp_path, DIVERGENT, query(pattern))) == [], pattern

    def test_a_single_bracket_posix_class_is_not_reported(self, tmp_path: pathlib.Path) -> None:
        # `[:alpha:]` without the outer bracket is an ordinary character set
        # and measured eight rows on both engines. Matching on `[:` rather
        # than `[[:` would report it.
        assert findings(build(tmp_path, DIVERGENT, query(r'r"[:alpha:]+"'))) == []

    def test_a_pattern_broken_on_both_engines_is_not_reported(self, tmp_path: pathlib.Path) -> None:
        # `\z`, `\Q...\E` and `\h` all raise on SQLite *and* Postgres. That is
        # a broken pattern, not a divergence, and reporting it under a
        # portability rule would tell the reader the wrong thing about why.
        for pattern in (r'r"USD\z"', r'r"\Qa.c\E"', r'r"a\hc"'):
            assert findings(build(tmp_path, DIVERGENT, query(pattern))) == [], pattern

    def test_a_postgres_only_project_is_not_reported(self, tmp_path: pathlib.Path) -> None:
        assert findings(build(tmp_path, POSTGRES_ONLY, query(r'r"\bUSD\b"'))) == []

    def test_the_divergence_is_what_makes_it_reportable(self, tmp_path: pathlib.Path) -> None:
        assert len(findings(build(tmp_path, DIVERGENT, query(r'r"\bUSD\b"')))) == 1

    def test_a_non_literal_pattern_is_not_inspected(self, tmp_path: pathlib.Path) -> None:
        # A name could hold anything. Reporting it would be guessing, and the
        # limitation says so rather than the rule pretending otherwise.
        code = (
            "from shop.models import Order\n\n"
            'PATTERN = r"\\bUSD\\b"\n\n\n'
            "def search():\n    return Order.objects.filter(sku__regex=PATTERN)\n"
        )
        assert findings(build(tmp_path, DIVERGENT, code)) == []

    def test_the_literal_control_proves_that_silence_is_aimed(self, tmp_path: pathlib.Path) -> None:
        # Presence control for the test above: the same pattern written
        # inline is reported.
        assert len(findings(build(tmp_path, DIVERGENT, query(r'r"\bUSD\b"')))) == 1

    def test_a_non_regex_lookup_carrying_the_same_text_is_not_reported(
        self, tmp_path: pathlib.Path
    ) -> None:
        # `\b` in a substring match is just two characters. The construct only
        # means anything to a regex engine.
        assert findings(build(tmp_path, DIVERGENT, query(r'r"\bUSD\b"', "sku__contains"))) == []


class TestShapesThatMustNotCrashIt:
    """`filter(**params)` gives a keyword with no name at all.

    Every guard in this rule tests `kw.arg is not None` first, and nothing
    proved those guards were load-bearing until this shape existed:
    dereferencing a `None` argument name raises, and a rule that raises is
    worse than one that misses, because it takes the rest of the file with it.
    """

    def test_a_double_star_call_is_survived(self, tmp_path: pathlib.Path) -> None:
        code = (
            "from shop.models import Order\n\n\n"
            "def search(params):\n    return Order.objects.filter(**params)\n"
        )
        assert findings(build(tmp_path, DIVERGENT, code)) == []

    def test_a_double_star_beside_a_real_lookup_still_reports(self, tmp_path: pathlib.Path) -> None:
        # Presence control: the unnamed keyword must be stepped over, not
        # treated as a reason to abandon the call.
        code = (
            "from shop.models import Order\n\n\n"
            "def search(params):\n"
            '    return Order.objects.filter(**params, sku__regex=r"\\bUSD\\b")\n'
        )
        assert len(findings(build(tmp_path, DIVERGENT, code))) == 1


class TestHowItReads:
    def test_the_iregex_lookup_is_read_too(self, tmp_path: pathlib.Path) -> None:
        found = findings(build(tmp_path, DIVERGENT, query(r'r"\bUSD\b"', "sku__iregex")))
        assert len(found) == 1

    def test_a_related_lookup_path_is_read(self, tmp_path: pathlib.Path) -> None:
        # The lookup is matched by suffix, so a traversal reaches it too.
        found = findings(build(tmp_path, DIVERGENT, query(r'r"\bUSD\b"', "vendor__name__regex")))
        assert len(found) == 1

    def test_two_constructs_in_one_pattern_are_listed_together(
        self, tmp_path: pathlib.Path
    ) -> None:
        found = findings(build(tmp_path, DIVERGENT, query(r'r"\bUSD\b[[:digit:]]+"')))
        assert len(found) == 1
        assert found[0].properties["constructs"] == "[[: \\b"
        # Asserting `" and " in message` here looked like it checked the join
        # and did not: the `[[:` clause contains its own "and", so the
        # assertion passed with the separator blanked entirely. That is also
        # why the separator is "; " -- joining two clauses that each already
        # contain "and" with a third produced an unreadable run-on.
        assert found[0].message == (
            "this regex is not portable: `\\b` matches a word boundary under Python's "
            "`re` and a literal backspace under Postgres' POSIX engine, so the pattern "
            "quietly matches different rows; `[[:` is a POSIX character class Postgres "
            "understands and Python's `re` reads as a nested set, so the pattern "
            "quietly matches different rows -- and Python raises a FutureWarning about "
            "it rather than an error"
        )

    def test_a_pattern_that_breaks_both_ways_records_both(self, tmp_path: pathlib.Path) -> None:
        found = findings(build(tmp_path, DIVERGENT, query(r'r"\y5\y(?P<w>a)"')))
        assert found[0].properties["breaks"] == "postgres sqlite"

    def test_two_calls_are_two_findings(self, tmp_path: pathlib.Path) -> None:
        code = (
            "from shop.models import Order\n\n\n"
            "def a():\n"
            '    return Order.objects.filter(sku__regex=r"\\bUSD\\b")\n\n\n'
            "def b():\n"
            '    return Order.objects.exclude(sku__regex=r"[[:digit:]]+")\n'
        )
        assert len(findings(build(tmp_path, DIVERGENT, code))) == 2

    def test_the_evidence_names_both_engines_mechanisms(self, tmp_path: pathlib.Path) -> None:
        config = [
            e
            for e in findings(build(tmp_path, DIVERGENT, query(r'r"\bUSD\b"')))[0].evidence
            if e.kind.value == "config"
        ]
        assert len(config) == 1
        assert config[0].content == (
            "sqlite:   django registers re.search as the REGEXP operator\n"
            "postgres: the ~ operator, POSIX ARE"
        )
        assert config[0].source == "measured against django 6.0"

    def test_the_source_evidence_quotes_the_call(self, tmp_path: pathlib.Path) -> None:
        source = [
            e
            for e in findings(build(tmp_path, DIVERGENT, query(r'r"\bUSD\b"')))[0].evidence
            if e.kind.value == "source"
        ]
        assert "sku__regex" in source[0].content
        assert source[0].source == "shop/queries.py"

    def test_the_whole_message_for_a_single_construct(self, tmp_path: pathlib.Path) -> None:
        # Written out rather than assembled from the table, so a wrong constant
        # in the table is a failure here rather than a matching pair.
        assert findings(build(tmp_path, DIVERGENT, query(r'r"\y555\y"')))[0].message == (
            "this regex is not portable: `\\y` is Postgres' word boundary and is not "
            "valid in Python's `re`, so the query raises OperationalError on SQLite"
        )

    def test_the_construct_is_quoted_in_the_message(self, tmp_path: pathlib.Path) -> None:
        message = findings(build(tmp_path, DIVERGENT, query(r'r"\bUSD\b"')))[0].message
        assert message.startswith("this regex is not portable: `\\b` matches")


class TestItIsStillTheSameRule:
    def test_it_is_registered_under_the_portability_family(self) -> None:
        from djaudit.models import Family
        from djaudit.registry import get

        assert get("DJX-008").meta.family is Family.DJX

    def test_the_rationale_cites_the_measured_counts(self) -> None:
        from djaudit.registry import get

        rationale = get("DJX-008").meta.rationale
        assert "matched one row on SQLite and zero on Postgres" in rationale
        assert "six rows on" in rationale

    def test_it_says_it_only_reads_literal_patterns(self) -> None:
        from djaudit.registry import get

        assert any(
            "written as a literal string" in limit for limit in get("DJX-008").meta.limitations
        )

    def test_it_says_it_is_not_a_warning_about_regexes_in_general(self) -> None:
        # The claim that makes the rule trustworthy: twenty constructs were
        # measured to agree and are deliberately silent.
        from djaudit.registry import get

        assert any(
            "not a warning about regular expressions in general" in limit
            for limit in get("DJX-008").meta.limitations
        )

    def test_the_remediation_admits_a_word_boundary_has_no_portable_spelling(
        self,
    ) -> None:
        from djaudit.registry import get

        assert "no portable" in get("DJX-008").meta.remediation

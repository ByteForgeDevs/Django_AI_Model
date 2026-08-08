"""Tests for `DJM-003`, and for the index operations it must stay quiet about."""

from __future__ import annotations

import pytest

from djaudit.models import Finding
from djaudit.rules.djm_addindex_blocking import BlockingAddIndex
from tests.rules.test_djm_addfield_not_null import Build, findings_for, migration, project

CREATE = """\
migrations.CreateModel(
    name='Order',
    fields=[
        ('id', models.AutoField(primary_key=True, serialize=False)),
        ('code', models.CharField(max_length=64)),
    ],
),
"""

ADD_INDEX = """\
migrations.AddIndex(
    model_name='order',
    index=models.Index(fields=['code'], name='shop_order_code_idx'),
),
"""


def run(make_project: Build, operation: str, atomic: bool | None = None) -> list[str]:
    """The operation in a leaf migration, against a table an earlier one made."""
    ctx = project(
        make_project,
        {
            "shop/migrations/0001_initial.py": migration(CREATE),
            "shop/migrations/0002_index.py": migration(
                operation, deps="('shop', '0001_initial')", atomic=atomic
            ),
        },
    )
    return findings_for(BlockingAddIndex(), ctx)


class TestItReportsABuildOverExistingRows:
    def test_an_add_index_on_a_populated_table(self, make_project: Build) -> None:
        """The whole message, written out.

        Asserting substrings left the punctuation holding it together
        unchecked: mutation blanked the `.` between app and migration name, so
        `shop.0002_index` became `shop0002_index`, and every substring test
        still passed. A citation a reader cannot paste back is a defect, so the
        sentence is pinned rather than sampled.
        """
        found = run(make_project, ADD_INDEX)

        assert found == [
            "`shop.0002_index` adds an index to `order`, which Postgres builds "
            "under a `SHARE` lock -- writes to the table block until it "
            "completes. The migration is atomic, so it also needs "
            "`atomic = False`."
        ]

    def test_two_indexes_in_one_migration_are_two_findings(self, make_project: Build) -> None:
        second = (
            "migrations.AddIndex(\n"
            "    model_name='order',\n"
            "    index=models.Index(fields=['id'], name='shop_order_id_idx'),\n"
            "),\n"
        )
        found = run(make_project, ADD_INDEX + second)

        assert len(found) == 2

    def test_a_table_whose_creator_was_never_seen_is_assumed_populated(
        self, make_project: Build
    ) -> None:
        """No `0001_initial` in this project, so the table predates the change.

        An app installed before this change is the dangerous case rather than
        the safe one, so an unknown creator counts as populated.
        """
        ctx = project(make_project, {"shop/migrations/0002_index.py": migration(ADD_INDEX)})

        assert len(findings_for(BlockingAddIndex(), ctx)) == 1


class TestItIsQuietWhereTheBuildIsFree:
    def test_an_index_built_in_the_creating_deploy(self, make_project: Build) -> None:
        """`CreateModel` and `AddIndex` in one still-pending migration.

        The table does not exist until this migration runs, so the index is
        built over nothing. This is the contrast to
        `test_a_table_whose_creator_was_never_seen_is_assumed_populated`: same
        operation, opposite verdict, and the difference is entirely whether the
        migration that creates the table is itself still pending.
        """
        ctx = project(
            make_project, {"shop/migrations/0001_initial.py": migration(CREATE + ADD_INDEX)}
        )

        assert findings_for(BlockingAddIndex(), ctx) == []


class TestItIsQuietOnEveryOtherIndexOperation:
    @pytest.mark.parametrize(
        ("operation", "why"),
        [
            pytest.param(
                "migrations.AddIndexConcurrently(\n"
                "    model_name='order',\n"
                "    index=models.Index(fields=['code'], name='shop_order_code_idx'),\n"
                "),\n",
                "it is the fix",
                id="AddIndexConcurrently",
            ),
            pytest.param(
                "migrations.RemoveIndex(model_name='order', name='shop_order_code_idx'),\n",
                "dropping an index takes a brief exclusive lock, not a build",
                id="RemoveIndex",
            ),
            pytest.param(
                "migrations.RenameIndex(\n"
                "    model_name='order', new_name='b', old_name='a',\n"
                "),\n",
                "renaming touches the catalogue only",
                id="RenameIndex",
            ),
            pytest.param(
                "migrations.AlterIndexTogether(name='order', index_together={('code',)}),\n",
                "it may build or drop, and this rule does not replay which",
                id="AlterIndexTogether",
            ),
        ],
    )
    def test_it_reports_nothing(self, make_project: Build, operation: str, why: str) -> None:
        assert run(make_project, operation) == [], why

    def test_a_schema_operation_is_not_an_index_one(self, make_project: Build) -> None:
        """The guard is on the operation name, so something adjacent must miss.

        `AddField` reaches `inspect` for every migration in scope exactly as
        `AddIndex` does, so a mutant dropping the name check would report it.
        """
        add_field = (
            "migrations.AddField(model_name='order', name='note', field=models.TextField()),\n"
        )

        assert run(make_project, add_field) == []


class TestItReportsTheSecondEditTheFixNeeds:
    def test_an_atomic_migration_is_told_to_stop_being_one(self, make_project: Build) -> None:
        found = run(make_project, ADD_INDEX, atomic=None)

        assert "it also needs `atomic = False`" in found[0]

    def test_a_migration_already_non_atomic_is_not(self, make_project: Build) -> None:
        """The contrast. Without it the sentence could be unconditional."""
        found = run(make_project, ADD_INDEX, atomic=False)

        assert "atomic" not in found[0]

    def test_atomic_true_written_out_is_still_atomic(self, make_project: Build) -> None:
        found = run(make_project, ADD_INDEX, atomic=True)

        assert "it also needs `atomic = False`" in found[0]


class TestTheEvidence:
    def _finding(self, make_project: Build, atomic: bool | None = None) -> Finding:
        ctx = project(
            make_project,
            {
                "shop/migrations/0001_initial.py": migration(CREATE),
                "shop/migrations/0002_index.py": migration(
                    ADD_INDEX, deps="('shop', '0001_initial')", atomic=atomic
                ),
            },
        )
        (finding,) = BlockingAddIndex().check(ctx)
        return finding

    def test_it_quotes_the_operation(self, make_project: Build) -> None:
        finding = self._finding(make_project)

        assert "AddIndex" in finding.evidence[0].content
        assert "shop_order_code_idx" in finding.evidence[0].content

    def test_it_records_the_atomic_flag(self, make_project: Build) -> None:
        """In full, including the half that says why the flag was recorded.

        `migration atomic=True` alone is a fact without a use. The clause after
        it is what tells the reader the flag is an obstacle to the fix rather
        than trivia, and a substring assertion never checked it was there.
        """
        finding = self._finding(make_project)

        assert finding.evidence[1].content == (
            "migration atomic=True; `AddIndexConcurrently` requires atomic=False"
        )

    def test_the_recorded_flag_follows_the_migration(self, make_project: Build) -> None:
        finding = self._finding(make_project, atomic=False)

        assert finding.evidence[1].content == (
            "migration atomic=False; `AddIndexConcurrently` requires atomic=False"
        )

    def test_the_evidence_cites_a_file_and_a_line(self, make_project: Build) -> None:
        """`path:line`, with the colon. Without it the citation is unusable.

        Cross-checked against the finding's own location rather than against a
        pinned line number, which depends on how the fixture lays the file out
        and would be re-pinned on every unrelated edit. The two are computed by
        different code -- `locate()` and the evidence f-string -- so agreement
        is a real check, and the separator is written literally here so blanking
        it in the rule still fails.
        """
        finding = self._finding(make_project)
        expected = finding.location.file + ":" + str(finding.location.line)

        assert finding.location.file == "shop/migrations/0002_index.py"
        assert finding.evidence[0].source == expected
        assert finding.evidence[1].source == expected

    def test_it_points_at_the_operation_not_the_file(self, make_project: Build) -> None:
        """Two operations in one file must not collapse to one location."""
        finding = self._finding(make_project)

        assert finding.location.file.endswith("0002_index.py")
        assert finding.location.line > 1

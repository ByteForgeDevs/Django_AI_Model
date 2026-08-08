"""Tests for `LiveContext`.

Every test builds a real Django project on disk and starts a real interpreter
against it. The module's job is to survive what real targets do -- print at
import time, fail to configure, hold a setting we cannot serialise -- and a
fake `Outcome` would only prove we can write a fake `Outcome`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from djaudit.live.context import (
    CLOSE,
    OPEN,
    QUESTIONS,
    SCRIPT,
    LiveContext,
    Unavailable,
    _payload,
    inspect_target,
)
from djaudit.live.runner import self_check

SECRET = "django-insecure-thisisthetopsecretvalue"
PASSWORD = "hunter2-the-database-password"

SETTINGS = f"""
SECRET_KEY = {SECRET!r}
DEBUG = True
INSTALLED_APPS = ["django.contrib.contenttypes", "django.contrib.auth"]
DATABASES = {{
    "default": {{
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "app",
        "USER": "admin",
        "PASSWORD": {PASSWORD!r},
        "HOST": "db.internal",
    }},
    "replica": {{"ENGINE": "django.db.backends.sqlite3", "NAME": "/tmp/r.sqlite3"}},
}}
ALLOWED_HOSTS = ["example.test"]
MIDDLEWARE = ["django.middleware.common.CommonMiddleware"]
USE_TZ = True
"""

MANAGE = """
import os, sys
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "proj.settings")
from django.core.management import execute_from_command_line
execute_from_command_line(sys.argv)
"""


def make_project(root: Path, settings: str = SETTINGS, manage: str = MANAGE) -> Path:
    """A minimal but genuine Django project. Returns its `manage.py`."""
    (root / "proj").mkdir(parents=True, exist_ok=True)
    (root / "proj" / "__init__.py").write_text("")
    (root / "proj" / "settings.py").write_text(settings)
    (root / "manage.py").write_text(manage)
    return root / "manage.py"


@pytest.fixture(scope="module")
def answered(tmp_path_factory: pytest.TempPathFactory) -> LiveContext:
    """One real interrogation, shared: each one starts Django from cold."""
    manage = make_project(tmp_path_factory.mktemp("answered"))
    result = inspect_target(self_check(), manage)
    assert isinstance(result, LiveContext), result.explain()
    return result


class TestItAsksTheTargetRatherThanGuessing:
    def test_it_reports_the_django_that_is_installed(self, answered: LiveContext) -> None:
        import django

        assert answered.django_version == django.get_version()

    def test_it_reports_the_settings_module_that_was_resolved(self, answered: LiveContext) -> None:
        """The one `manage.py` chose, not the one we would have chosen."""
        assert answered.settings_module == "proj.settings"

    def test_it_reports_the_python_the_target_runs_on(self, answered: LiveContext) -> None:
        assert answered.python_version == self_check().version

    def test_it_reads_settings_the_static_tier_cannot(self, answered: LiveContext) -> None:
        assert answered.debug is True
        assert answered.use_tz is True
        assert answered.allowed_hosts == ("example.test",)
        assert answered.middleware == ("django.middleware.common.CommonMiddleware",)

    def test_it_reports_a_default_django_supplies_rather_than_the_project(
        self, answered: LiveContext
    ) -> None:
        """`DEFAULT_AUTO_FIELD` is unset in the project, so this is Django's
        own answer -- exactly the kind of fact the static tier cannot see."""
        assert answered.default_auto_field == "django.db.models.BigAutoField"

    def test_it_reports_every_database_alias(self, answered: LiveContext) -> None:
        assert answered.databases == (
            ("default", "django.db.backends.postgresql"),
            ("replica", "django.db.backends.sqlite3"),
        )

    def test_it_reports_the_apps_the_project_actually_loaded(self, answered: LiveContext) -> None:
        """Asserted literally rather than against `INSTALLED_APPS`: a list built
        from the value under test agrees with itself whatever the value is."""
        assert answered.installed_apps == (
            "django.contrib.contenttypes",
            "django.contrib.auth",
        )

    def test_a_healthy_target_reports_no_problems(self, answered: LiveContext) -> None:
        assert answered.problems == ()

    def test_it_is_available(self, answered: LiveContext) -> None:
        assert answered.available

    def test_it_explains_itself_in_one_line(self, answered: LiveContext) -> None:
        assert answered.explain() == (
            f"Django {answered.django_version} on Python {answered.python_version}; "
            "settings proj.settings; "
            "default via django.db.backends.postgresql, replica via django.db.backends.sqlite3"
        )


class TestItNeverCollectsASecret:
    """Evidence is written into SARIF and uploaded to code scanning.

    `manage.py diffsettings` would answer nearly every question in one call and
    would carry `SECRET_KEY` and every database password with it. A security
    tool that copies production credentials into a security dashboard has
    created a worse problem than any it can report.
    """

    def test_the_secret_key_is_not_in_the_result(self, answered: LiveContext) -> None:
        assert SECRET not in repr(answered)

    def test_the_database_password_is_not_in_the_result(self, answered: LiveContext) -> None:
        assert PASSWORD not in repr(answered)

    def test_the_password_never_crosses_the_pipe_at_all(self) -> None:
        """Reduced on the target's side, so it is not received and dropped."""
        rendered = SCRIPT.format(manage="manage.py", questions=QUESTIONS, open=OPEN, close=CLOSE)
        assert "PASSWORD" not in rendered
        for _, expression in QUESTIONS:
            assert "SECRET_KEY" not in expression

    def test_databases_is_reduced_to_the_engine(self, answered: LiveContext) -> None:
        for _, engine in answered.databases:
            assert engine.startswith("django.db.backends.")

    @pytest.mark.parametrize(
        "forbidden",
        ["SECRET_KEY", "PASSWORD", "API_KEY", "TOKEN", "diffsettings"],
    )
    def test_no_question_asks_for_a_credential(self, forbidden: str) -> None:
        """A regression gate on the allowlist itself, not on today's answers."""
        for name, expression in QUESTIONS:
            assert forbidden not in name.upper()
            assert forbidden not in expression


class TestItRunsTheProjectsOwnManagePy:
    """Why this is `runpy` plus an interception rather than `shell -c`."""

    def test_a_postgres_project_answers_without_a_database_driver(self, tmp_path: Path) -> None:
        """The defect that changed the design.

        `manage.py shell` loads the app registry and opens the database
        backend, so this project -- Postgres `ENGINE`, `psycopg` not installed,
        no server anywhere -- could not report its own Django version. That is
        the state every corpus is in under CI, and none of these questions
        needs a database.
        """
        manage = make_project(tmp_path)
        result = inspect_target(self_check(), manage)
        assert isinstance(result, LiveContext), result.explain()
        assert result.django_version is not None
        assert result.uses("postgresql")
        assert result.problems == ()

    def test_what_manage_py_does_before_django_still_happens(self, tmp_path: Path) -> None:
        """Projects read `.env` files and pick a settings module there.

        Running the file rather than reading it is the whole point: this is the
        work that chooses which settings exist.
        """
        manage = make_project(
            tmp_path,
            settings="import os\n" + SETTINGS + "\nALLOWED_HOSTS = [os.environ['CHOSEN_HOST']]\n",
            manage=(
                "import os, sys\n"
                "os.environ['CHOSEN_HOST'] = 'set-by-manage-py'\n"
                "os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'proj.settings')\n"
                "from django.core.management import execute_from_command_line\n"
                "execute_from_command_line(sys.argv)\n"
            ),
        )
        result = inspect_target(self_check(), manage)
        assert isinstance(result, LiveContext), result.explain()
        assert result.allowed_hosts == ("set-by-manage-py",)

    def test_a_manage_py_guarded_by_a_main_block_is_still_run(self, tmp_path: Path) -> None:
        """`run_name='__main__'`, or the common layout would do nothing at all."""
        manage = make_project(
            tmp_path,
            settings="import os\n" + SETTINGS + "\nALLOWED_HOSTS = [os.environ['CHOSEN_HOST']]\n",
            manage=(
                "import os, sys\n"
                "def main():\n"
                "    os.environ['CHOSEN_HOST'] = 'from-main'\n"
                "    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'proj.settings')\n"
                "    from django.core.management import execute_from_command_line\n"
                "    execute_from_command_line(sys.argv)\n"
                "if __name__ == '__main__':\n"
                "    main()\n"
            ),
        )
        result = inspect_target(self_check(), manage)
        assert isinstance(result, LiveContext), result.explain()
        assert result.allowed_hosts == ("from-main",)

    def test_the_management_command_itself_never_runs(self, tmp_path: Path) -> None:
        """Interception, not execution: nothing after that call may happen."""
        manage = make_project(
            tmp_path,
            manage=MANAGE + "\nopen('ran.marker', 'w').write('x')\n",
        )
        result = inspect_target(self_check(), manage)
        assert isinstance(result, LiveContext), result.explain()
        assert not (tmp_path / "ran.marker").exists()

    def test_a_manage_py_that_exits_is_not_an_error(self, tmp_path: Path) -> None:
        manage = make_project(
            tmp_path,
            manage=(
                "import os, sys\n"
                "os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'proj.settings')\n"
                "sys.exit(0)\n"
            ),
        )
        result = inspect_target(self_check(), manage)
        assert isinstance(result, LiveContext), result.explain()
        assert result.settings_module == "proj.settings"


class TestTheTargetOwnsStdout:
    def test_a_project_that_prints_on_import_does_not_corrupt_the_answer(
        self, tmp_path: Path
    ) -> None:
        """Measured, not feared: one `print` in `settings.py` lands in front of
        the payload even at `shell -v 0`."""
        manage = make_project(tmp_path, settings='print("loading production config")\n' + SETTINGS)
        result = inspect_target(self_check(), manage)
        assert isinstance(result, LiveContext)
        assert result.settings_module == "proj.settings"

    def test_a_project_that_prints_something_json_shaped_is_still_read(
        self, tmp_path: Path
    ) -> None:
        """The delimiters are what make this a lookup rather than a guess."""
        noise = 'print(\'{"answers": {"django_version": "0.0.1"}}\')\n'
        manage = make_project(tmp_path, settings=noise + SETTINGS)
        result = inspect_target(self_check(), manage)
        assert isinstance(result, LiveContext)
        assert result.django_version != "0.0.1"

    def test_output_after_the_payload_is_ignored_too(self, tmp_path: Path) -> None:
        manage = make_project(
            tmp_path,
            manage=MANAGE + '\nprint("goodbye")\n',
        )
        result = inspect_target(self_check(), manage)
        assert isinstance(result, LiveContext)


class TestWhenTheTargetCannotAnswer:
    def test_a_settings_module_that_raises_is_reported_not_propagated(self, tmp_path: Path) -> None:
        manage = make_project(tmp_path, settings="raise RuntimeError('settings blew up')\n")
        result = inspect_target(self_check(), manage)
        assert isinstance(result, Unavailable)
        assert result.explain() == (
            "the target's Django did not start: RuntimeError: settings blew up"
        )

    def test_the_last_line_is_quoted_because_the_first_thousand_are_django(
        self, tmp_path: Path
    ) -> None:
        """A traceback names Django's import machinery above the one line the
        reader wrote and can fix."""
        manage = make_project(tmp_path, settings="import nonexistent_module_xyz\n")
        result = inspect_target(self_check(), manage)
        assert isinstance(result, Unavailable)
        assert "nonexistent_module_xyz" in result.detail
        assert "\n" not in result.detail

    def test_a_missing_manage_py_is_named(self, tmp_path: Path) -> None:
        missing = tmp_path / "manage.py"
        result = inspect_target(self_check(), missing)
        assert isinstance(result, Unavailable)
        assert result.explain() == f"no manage.py: {missing} is not a file"

    def test_a_directory_is_not_a_manage_py(self, tmp_path: Path) -> None:
        (tmp_path / "manage.py").mkdir()
        assert isinstance(inspect_target(self_check(), tmp_path / "manage.py"), Unavailable)

    def test_it_does_not_start_django_to_find_out_the_file_is_missing(self, tmp_path: Path) -> None:
        """Cheap checks first: starting Django costs seconds."""
        import time

        started = time.monotonic()
        inspect_target(self_check(), tmp_path / "manage.py")
        assert time.monotonic() - started < 1

    def test_a_target_that_hangs_is_reported_as_a_timeout(self, tmp_path: Path) -> None:
        manage = make_project(tmp_path, settings="import time\ntime.sleep(120)\n" + SETTINGS)
        result = inspect_target(self_check(), manage, timeout=3.0)
        assert isinstance(result, Unavailable)
        assert result.explain() == (
            "the target did not respond in time: its manage.py was killed after 3s"
        )

    def test_unavailable_is_not_available(self) -> None:
        assert not Unavailable("x").available

    def test_a_reason_without_detail_reads_as_a_sentence(self) -> None:
        assert Unavailable("the target's Django did not start").explain() == (
            "the target's Django did not start"
        )


class TestOneBadAnswerDoesNotCostTheRest:
    def test_a_setting_that_raises_leaves_the_others_intact(self, tmp_path: Path) -> None:
        """`DATABASES` built from an env var that is not set is the common case."""
        hostile = (
            SETTINGS + "\nclass _Explodes:\n"
            "    def __iter__(self): raise ValueError('cannot read MIDDLEWARE')\n"
            "MIDDLEWARE = _Explodes()\n"
        )
        manage = make_project(tmp_path, settings=hostile)
        result = inspect_target(self_check(), manage)
        assert isinstance(result, LiveContext)
        assert result.django_version is not None
        assert result.middleware == ()
        assert result.problems == ("middleware: ValueError: cannot read MIDDLEWARE",)

    def test_a_value_that_will_not_serialise_arrives_as_its_repr(self, tmp_path: Path) -> None:
        """`default=str`: an unserialisable setting is not worth losing the run."""
        manage = make_project(
            tmp_path,
            settings=SETTINGS + "\nfrom pathlib import Path\nALLOWED_HOSTS = [Path('/x')]\n",
        )
        result = inspect_target(self_check(), manage)
        assert isinstance(result, LiveContext)
        assert result.allowed_hosts == ("/x",)


class TestReadingTheEngines:
    def test_engines_collapses_aliases(self, answered: LiveContext) -> None:
        assert answered.engines == {
            "django.db.backends.postgresql",
            "django.db.backends.sqlite3",
        }

    def test_uses_matches_the_backend_name(self, answered: LiveContext) -> None:
        assert answered.uses("postgresql")
        assert answered.uses("sqlite3")
        assert not answered.uses("mysql")

    def test_uses_matches_a_gis_backend_as_its_base(self) -> None:
        """`postgis` is Postgres, and a rule about Postgres locks must see it."""
        context = LiveContext(
            interpreter=self_check(),
            databases=(("default", "django.contrib.gis.db.backends.postgis"),),
        )
        assert context.uses("postgis")

    def test_uses_does_not_match_a_prefix(self) -> None:
        context = LiveContext(
            interpreter=self_check(),
            databases=(("default", "django.db.backends.postgresql"),),
        )
        assert not context.uses("postgres")

    def test_an_alias_with_no_engine_is_dropped(self) -> None:
        assert LiveContext(interpreter=self_check(), databases=(("default", ""),)).engines == set()


class TestReadingThePayload:
    def test_it_finds_the_payload_inside_noise(self) -> None:
        assert _payload(f'hello\n{OPEN}{{"answers": {{}}}}{CLOSE}\nbye') == {"answers": {}}

    def test_no_payload_at_all(self) -> None:
        assert _payload("Traceback (most recent call last):") is None

    def test_an_opening_delimiter_with_no_close(self) -> None:
        assert _payload(f'{OPEN}{{"answers": {{}}}}') is None

    def test_a_truncated_payload_is_not_read_as_a_whole_one(self) -> None:
        """The runner truncates at `OUTPUT_LIMIT`, so a half-payload is a real
        arrival, not a hypothetical. Here the JSON happens to be complete at the
        cut and only the closing frame is lost: without the `end < 0` guard the
        slice to `-1` trims the newline and parses, and djaudit reports a
        truncated reply as the target's full answer."""
        assert _payload(f'{OPEN}{{"answers": {{}}}}\n') is None

    def test_a_closing_delimiter_with_no_open(self) -> None:
        assert _payload(f'{{"answers": {{}}}}{CLOSE}') is None

    def test_unframed_output_is_not_read_as_a_payload(self) -> None:
        """Padded so the unguarded slice would land exactly on the JSON. The
        opening frame is the only thing separating our answer from whatever the
        target chose to print, so finding JSON is not finding a payload."""
        assert _payload("X" * (len(OPEN) - 1) + '{"answers": {}}' + CLOSE) is None

    def test_a_payload_that_is_not_json(self) -> None:
        assert _payload(f"{OPEN}not json at all{CLOSE}") is None

    def test_a_payload_that_is_json_but_not_an_object(self) -> None:
        assert _payload(f"{OPEN}[1, 2, 3]{CLOSE}") is None

    def test_an_empty_payload(self) -> None:
        assert _payload(f"{OPEN}{CLOSE}") is None

    def test_the_delimiters_are_distinct(self) -> None:
        assert OPEN != CLOSE


class TestAReplyOfTheWrongShape:
    """The target writes to the same stdout we read, and it runs first, so the
    frame it sees is a frame it can forge. We cannot stop that -- it is the
    target's own interpreter -- but a forged reply must be declined rather than
    crash the audit on the first attribute access."""

    @staticmethod
    def forging(tmp_path: Path, payload: object) -> Unavailable | LiveContext:
        forged = f"{OPEN}{json.dumps(payload)}{CLOSE}"
        manage = make_project(tmp_path, settings=f"print({forged!r})\n" + SETTINGS)
        return inspect_target(self_check(), manage)

    def test_answers_must_be_a_mapping(self, tmp_path: Path) -> None:
        result = self.forging(tmp_path, {"answers": ["not", "a", "mapping"], "problems": {}})
        assert isinstance(result, Unavailable)
        assert result.explain() == "the target's reply was not in the expected shape"

    def test_problems_must_be_a_mapping_too(self, tmp_path: Path) -> None:
        """Checked separately: `problems` is read with `.items()` further down,
        so a list there is the same crash one line later."""
        result = self.forging(tmp_path, {"answers": {}, "problems": ["broken"]})
        assert isinstance(result, Unavailable)

    def test_a_forged_frame_is_read_before_our_own(self, tmp_path: Path) -> None:
        """The control for the two above: they only mean something if the forged
        frame is what `inspect_target` actually parsed."""
        result = self.forging(tmp_path, {"answers": {"django_version": "0.0-forged"}})
        assert isinstance(result, LiveContext)
        assert result.django_version == "0.0-forged"

    def test_the_payload_itself_is_returned_intact(self) -> None:
        """`_payload` frames and parses; judging the shape is not its job."""
        forged = json.dumps({"answers": ["not", "a", "mapping"], "problems": {}})
        assert _payload(f"{OPEN}{forged}{CLOSE}") == {
            "answers": ["not", "a", "mapping"],
            "problems": {},
        }


class TestTheQuestionList:
    def test_every_question_has_a_unique_name(self) -> None:
        names = [name for name, _ in QUESTIONS]
        assert len(names) == len(set(names))

    def test_every_answer_has_a_field_to_land_in(self) -> None:
        fields = set(LiveContext._fields)
        for name, _ in QUESTIONS:
            assert name in fields, f"{name} is asked for but never stored"

    def test_the_questions_are_expressions_not_statements(self) -> None:
        for _, expression in QUESTIONS:
            compile(expression, "<question>", "eval")

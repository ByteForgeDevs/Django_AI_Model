"""Tests for interpreter detection.

The property worth the most here is a refusal rather than a discovery.
djaudit runs from a virtualenv of its own, so `$VIRTUAL_ENV` is usually set and
usually points at ours; following it would run the target's code against our
Django and produce findings that look entirely normal. That is measured in
`TestItRefusesOurOwnEnvironment`.

`TestAgainstRealEnvironments` builds an environment with the actual tool rather
than writing `pyvenv.cfg` by hand, because a hand-written fixture only proves
we can read what we ourselves wrote.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from djaudit.live.interpreter import (
    CANDIDATE_DIRECTORIES,
    CONFIG,
    Creator,
    Rejection,
    creator_of,
    executable_in,
    find_interpreter,
    read_config,
)

UV_CONFIG = """\
home = /usr/bin
implementation = CPython
uv = 0.12.1
version_info = 3.13.9
include-system-site-packages = false
"""


def make_env(prefix: Path, config: str = UV_CONFIG, executable: bool = True) -> Path:
    """An environment that looks real to everything but an execve."""
    prefix.mkdir(parents=True, exist_ok=True)
    (prefix / CONFIG).write_text(config)
    if executable:
        binary = prefix / "bin" / "python"
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o755)
    return prefix


class TestItFindsAnEnvironmentInTheTarget:
    @pytest.mark.parametrize("name", CANDIDATE_DIRECTORIES)
    def test_each_conventional_directory_is_tried(self, tmp_path: Path, name: str) -> None:
        make_env(tmp_path / name)
        search = find_interpreter(tmp_path, environ={})
        assert search.interpreter is not None
        assert search.interpreter.prefix == tmp_path / name

    def test_it_reports_the_executable_and_not_the_prefix(self, tmp_path: Path) -> None:
        make_env(tmp_path / ".venv")
        search = find_interpreter(tmp_path, environ={})
        assert search.interpreter is not None
        assert search.interpreter.executable == tmp_path / ".venv" / "bin" / "python"

    def test_dot_venv_wins_over_the_older_conventions(self, tmp_path: Path) -> None:
        """A project with both is usually one that migrated to `uv`."""
        make_env(tmp_path / ".venv")
        make_env(tmp_path / "venv")
        search = find_interpreter(tmp_path, environ={})
        assert search.interpreter is not None
        assert search.interpreter.prefix.name == ".venv"

    def test_a_windows_layout_is_found_on_any_host(self, tmp_path: Path) -> None:
        """The host running djaudit need not be the host that made the venv."""
        prefix = tmp_path / ".venv"
        prefix.mkdir()
        (prefix / CONFIG).write_text(UV_CONFIG)
        binary = prefix / "Scripts" / "python.exe"
        binary.parent.mkdir()
        binary.write_text("")
        binary.chmod(0o755)
        search = find_interpreter(tmp_path, environ={})
        assert search.interpreter is not None
        assert search.interpreter.executable == binary

    def test_python3_is_accepted_when_python_is_gone(self, tmp_path: Path) -> None:
        """A distro upgrade that leaves `python3` behind and breaks `python`.

        Every venv is created with all three names, so this only ever happens
        to a damaged one -- which is exactly when a clear answer is worth most.
        """
        prefix = make_env(tmp_path / ".venv")
        (prefix / "bin" / "python").unlink()
        binary = prefix / "bin" / "python3"
        binary.write_text("")
        binary.chmod(0o755)
        search = find_interpreter(tmp_path, environ={})
        assert search.interpreter is not None
        assert search.interpreter.executable == binary

    def test_the_provenance_says_where_it_came_from(self, tmp_path: Path) -> None:
        make_env(tmp_path / ".venv")
        search = find_interpreter(tmp_path, environ={})
        assert search.interpreter is not None
        assert search.interpreter.found_by == ".venv/ in the target"


class TestItRefusesOurOwnEnvironment:
    """The failure that does not look like one.

    Running the target under djaudit's interpreter does not crash. It reports
    on our Django, our settings and our installed packages, and every finding
    looks perfectly ordinary.
    """

    def test_virtual_env_pointing_outside_the_target_is_refused(self, tmp_path: Path) -> None:
        outside = make_env(tmp_path / "elsewhere" / ".venv")
        target = tmp_path / "target"
        target.mkdir()
        search = find_interpreter(target, environ={"VIRTUAL_ENV": str(outside)})
        assert not search.found
        assert "probably djaudit's" in search.explain()

    def test_virtual_env_inside_the_target_is_accepted(self, tmp_path: Path) -> None:
        """The one case where it is the right answer: an activated target."""
        prefix = make_env(tmp_path / "tooling" / "py")
        search = find_interpreter(tmp_path, environ={"VIRTUAL_ENV": str(prefix)})
        assert search.interpreter is not None
        assert search.interpreter.found_by == "$VIRTUAL_ENV, inside the target"

    def test_djaudits_own_prefix_is_refused_even_inside_the_target(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A checkout underneath our own environment is contrived but fatal."""
        prefix = make_env(tmp_path / ".venv")
        monkeypatch.setattr(sys, "prefix", str(prefix))
        search = find_interpreter(tmp_path, environ={})
        assert not search.found
        assert "djaudit's own environment" in search.explain()

    def test_the_refusal_survives_a_symlinked_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`$VIRTUAL_ENV` is routinely a different spelling of `sys.prefix`."""
        real = make_env(tmp_path / "real")
        link = tmp_path / "link"
        link.symlink_to(real)
        monkeypatch.setattr(sys, "prefix", str(real))
        search = find_interpreter(tmp_path, environ={"VIRTUAL_ENV": str(link)})
        assert not search.found
        assert "djaudit's own environment" in search.explain()

    def test_one_environment_is_not_reported_as_two_problems(self, tmp_path: Path) -> None:
        """`$VIRTUAL_ENV` routinely names a directory already tried by name."""
        prefix = make_env(tmp_path / ".venv", executable=False)
        search = find_interpreter(tmp_path, environ={"VIRTUAL_ENV": str(prefix)})
        assert len(search.rejected) == 1


class TestWhatIsNotAnEnvironment:
    def test_a_directory_named_venv_without_the_marker_is_not_one(self, tmp_path: Path) -> None:
        """PEP 405 defines a virtualenv by `pyvenv.cfg`, not by a name."""
        (tmp_path / ".venv" / "bin").mkdir(parents=True)
        search = find_interpreter(tmp_path, environ={})
        assert not search.found
        assert f"no {CONFIG}" in search.explain()

    def test_a_config_without_home_is_not_one(self, tmp_path: Path) -> None:
        """`home` is the one key all three creators write."""
        make_env(tmp_path / ".venv", config="uv = 0.12.1\n")
        assert not find_interpreter(tmp_path, environ={}).found

    def test_a_config_with_no_interpreter_is_refused(self, tmp_path: Path) -> None:
        make_env(tmp_path / ".venv", executable=False)
        search = find_interpreter(tmp_path, environ={})
        assert not search.found
        assert search.rejected == (
            Rejection(tmp_path / ".venv", "has pyvenv.cfg but no runnable interpreter in it"),
        )

    def test_a_python_that_is_not_executable_is_refused(self, tmp_path: Path) -> None:
        """An environment restored from an archive that dropped its bits."""
        prefix = make_env(tmp_path / ".venv")
        (prefix / "bin" / "python").chmod(0o644)
        assert not find_interpreter(tmp_path, environ={}).found

    def test_a_directory_is_not_mistaken_for_the_interpreter(self, tmp_path: Path) -> None:
        prefix = tmp_path / ".venv"
        prefix.mkdir()
        (prefix / CONFIG).write_text(UV_CONFIG)
        (prefix / "bin" / "python").mkdir(parents=True)
        assert not find_interpreter(tmp_path, environ={}).found

    def test_a_rejected_candidate_does_not_stop_a_later_one(self, tmp_path: Path) -> None:
        (tmp_path / ".venv").mkdir()
        make_env(tmp_path / "venv")
        search = find_interpreter(tmp_path, environ={})
        assert search.found
        assert search.rejected

    def test_nothing_at_all_is_reported_as_such(self, tmp_path: Path) -> None:
        search = find_interpreter(tmp_path, environ={})
        assert not search.found
        assert search.rejected == ()
        assert "no usable interpreter found in the target" in search.explain()

    def test_the_explanation_leads_with_what_was_searched(self, tmp_path: Path) -> None:
        """A bare list of rejections reads as though nothing else was tried.

        Asserted whole rather than by substring: this string is the entire
        report a user gets when the live tier does not come up, so its
        punctuation and its ordering are the deliverable.
        """
        (tmp_path / ".venv").mkdir()
        assert find_interpreter(tmp_path, environ={}).explain() == (
            "no usable interpreter found in the target "
            "(.venv, venv, env, .virtualenv were tried); "
            "live-tier rules will not run. "
            f"{tmp_path / '.venv'}: no pyvenv.cfg, so not a virtual environment."
        )

    def test_the_explanation_of_an_empty_target_is_the_lead_alone(self, tmp_path: Path) -> None:
        assert find_interpreter(tmp_path, environ={}).explain() == (
            "no usable interpreter found in the target "
            "(.venv, venv, env, .virtualenv were tried); "
            "live-tier rules will not run."
        )

    def test_a_config_that_is_not_utf8_is_read_rather_than_raising(self, tmp_path: Path) -> None:
        """A `pyvenv.cfg` written by a tool under a different locale.

        Undecodable bytes must not take the whole run down, and the keys
        around them are still readable.
        """
        prefix = tmp_path / ".venv"
        prefix.mkdir()
        (prefix / CONFIG).write_bytes(b"home = /usr/bin\nprompt = caf\xe9\n")
        binary = prefix / "bin" / "python"
        binary.parent.mkdir()
        binary.write_text("")
        binary.chmod(0o755)
        assert find_interpreter(tmp_path, environ={}).found


class TestEnvironmentsItDeclinesToGuessAt:
    """Kept outside the project under a path-derived hash.

    Globbing the name half and taking a single match would silently attach to a
    different checkout of the same project, which is the mistake this module
    exists to prevent.
    """

    def test_poetry_is_named_rather_than_guessed(self, tmp_path: Path) -> None:
        (tmp_path / "poetry.lock").write_text("")
        assert "poetry env info --path" in find_interpreter(tmp_path, environ={}).explain()

    def test_pipenv_is_named_rather_than_guessed(self, tmp_path: Path) -> None:
        (tmp_path / "Pipfile.lock").write_text("")
        assert "pipenv --venv" in find_interpreter(tmp_path, environ={}).explain()

    def test_tox_says_why_choosing_one_would_be_wrong(self, tmp_path: Path) -> None:
        (tmp_path / "tox.ini").write_text("")
        assert "Python version at random" in find_interpreter(tmp_path, environ={}).explain()

    def test_a_pyproject_alone_defers_nothing(self, tmp_path: Path) -> None:
        """It says nothing about where an environment lives."""
        (tmp_path / "pyproject.toml").write_text("")
        assert find_interpreter(tmp_path, environ={}).deferred == ()

    def test_an_in_project_environment_beats_a_deferral(self, tmp_path: Path) -> None:
        (tmp_path / "poetry.lock").write_text("")
        make_env(tmp_path / ".venv")
        assert find_interpreter(tmp_path, environ={}).found


class TestReadingTheConfig:
    def test_a_value_containing_an_equals_sign_survives(self, tmp_path: Path) -> None:
        """stdlib `venv` records the whole command line under `command`."""
        prefix = make_env(
            tmp_path / ".venv",
            config="home = /usr/bin\ncommand = /usr/bin/python3 -m venv --prompt a=b /tmp/x\n",
        )
        config = read_config(prefix)
        assert config is not None
        assert config["command"] == "/usr/bin/python3 -m venv --prompt a=b /tmp/x"

    def test_a_missing_config_reads_as_none(self, tmp_path: Path) -> None:
        assert read_config(tmp_path) is None

    def test_a_line_with_no_separator_is_skipped(self, tmp_path: Path) -> None:
        prefix = make_env(tmp_path / ".venv", config="home = /usr/bin\ngarbage\n")
        assert read_config(prefix) == {"home": "/usr/bin"}

    def test_keys_are_matched_case_insensitively(self, tmp_path: Path) -> None:
        prefix = make_env(tmp_path / ".venv", config="HOME = /usr/bin\nUV = 0.12.1\n")
        assert read_config(prefix) == {"home": "/usr/bin", "uv": "0.12.1"}

    def test_an_unreadable_config_reads_as_none(self, tmp_path: Path) -> None:
        """A directory where the file should be, which `read_text` raises on."""
        (tmp_path / CONFIG).mkdir()
        assert read_config(tmp_path) is None

    def test_executable_in_returns_none_for_an_empty_prefix(self, tmp_path: Path) -> None:
        assert executable_in(tmp_path) is None


class TestIdentifyingTheCreator:
    """Each tool writes one key the others do not, measured by making an
    environment with each rather than by reading their documentation."""

    def test_uv_is_identified_by_its_own_key(self) -> None:
        assert creator_of({"home": "/usr/bin", "uv": "0.12.1"}) is Creator.UV

    def test_virtualenv_is_identified_by_its_own_key(self) -> None:
        assert creator_of({"home": "/usr/bin", "virtualenv": "20.35.4"}) is Creator.VIRTUALENV

    def test_stdlib_venv_is_identified_by_command(self) -> None:
        assert creator_of({"home": "/usr/bin", "command": "python -m venv x"}) is Creator.VENV

    def test_uv_wins_over_virtualenv_when_both_keys_are_present(self) -> None:
        """`uv` can build on top of `virtualenv`'s layout."""
        config = {"home": "/usr/bin", "uv": "0.12.1", "virtualenv": "20.35.4"}
        assert creator_of(config) is Creator.UV

    def test_the_names_are_a_wire_format(self) -> None:
        """They are written into diagnostics, so renaming one is a break."""
        assert [c.value for c in Creator] == ["venv", "virtualenv", "uv", "unknown"]

    def test_an_unknown_creator_is_still_usable(self, tmp_path: Path) -> None:
        """A `pyvenv.cfg` we cannot attribute is still a virtual environment."""
        make_env(tmp_path / ".venv", config="home = /usr/bin\n")
        search = find_interpreter(tmp_path, environ={})
        assert search.interpreter is not None
        assert search.interpreter.creator is Creator.UNKNOWN

    def test_the_version_is_read_from_the_stdlib_spelling(self, tmp_path: Path) -> None:
        make_env(tmp_path / ".venv", config="home = /usr/bin\nversion = 3.13.9\n")
        search = find_interpreter(tmp_path, environ={})
        assert search.interpreter is not None
        assert search.interpreter.version == "3.13.9"

    def test_the_version_is_read_from_the_other_spelling(self, tmp_path: Path) -> None:
        """`uv` and `virtualenv` write `version_info` instead."""
        make_env(tmp_path / ".venv", config="home = /usr/bin\nversion_info = 3.12.1\n")
        search = find_interpreter(tmp_path, environ={})
        assert search.interpreter is not None
        assert search.interpreter.version == "3.12.1"

    def test_a_version_is_optional(self, tmp_path: Path) -> None:
        make_env(tmp_path / ".venv", config="home = /usr/bin\n")
        search = find_interpreter(tmp_path, environ={})
        assert search.interpreter is not None
        assert search.interpreter.version is None


@pytest.mark.skipif(sys.platform == "win32", reason="posix layout assumed by the builder")
class TestAgainstARealEnvironment:
    """Built with the real tool. A hand-written `pyvenv.cfg` only proves we can
    read what we ourselves wrote."""

    @pytest.fixture
    def real_project(self, tmp_path: Path) -> Path:
        target = tmp_path / "project"
        target.mkdir()
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", str(target / ".venv")],
            check=True,
            capture_output=True,
            timeout=120,
        )
        return target

    def test_it_is_found_and_attributed(self, real_project: Path) -> None:
        search = find_interpreter(real_project, environ={})
        assert search.interpreter is not None
        assert search.interpreter.creator is Creator.VENV
        assert search.interpreter.executable.is_file()

    def test_its_version_is_the_one_that_built_it(self, real_project: Path) -> None:
        search = find_interpreter(real_project, environ={})
        assert search.interpreter is not None
        assert search.interpreter.version is not None
        assert search.interpreter.version.startswith(
            f"{sys.version_info.major}.{sys.version_info.minor}"
        )

    def test_the_explanation_names_the_executable(self, real_project: Path) -> None:
        search = find_interpreter(real_project, environ={})
        assert search.interpreter is not None
        assert search.explain() == (
            f"live tier using {search.interpreter.executable} (.venv/ in the target)"
        )


class TestItNeverExecutesTheTarget:
    """Detection runs against repositories nobody has vetted."""

    def test_a_python_in_the_environment_is_not_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        witness = tmp_path / "ran"
        prefix = make_env(tmp_path / ".venv")
        (prefix / "bin" / "python").write_text(f"#!/bin/sh\ntouch {witness}\n")
        (prefix / "bin" / "python").chmod(0o755)

        def refuse(*args: object, **kwargs: object) -> None:
            raise AssertionError("detection must not spawn a process")

        monkeypatch.setattr(subprocess, "run", refuse)
        monkeypatch.setattr(subprocess, "Popen", refuse)
        monkeypatch.setattr(os, "system", refuse)
        assert find_interpreter(tmp_path, environ={}).found
        assert not witness.exists()

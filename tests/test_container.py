"""Tests for the container image in `Dockerfile`.

There is no docker on the machine this was written on, so nothing here builds
an image. CI does that, and running the built image against a fixture is the
only end-to-end evidence that exists.

What is left is still worth testing, and it is the part that fails most often:
the build context. `COPY pyproject.toml README.md ./` plus `COPY src ./src` is
a claim that those paths are sufficient to build a wheel, and that claim breaks
silently the day the packaging metadata starts reading a file nobody copied --
a `LICENSE` in `project.license-files`, a version read from a module outside
`src/`. `TestTheBuildContext` copies exactly what the Dockerfile copies into an
empty directory and builds there, which reproduces that failure locally in the
seconds it takes to build a wheel.

The rest reads the Dockerfile as data and checks it against the CLI, so an
entrypoint naming a command that has been renamed fails here rather than in a
user's pipeline.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import check_dockerfile

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "Dockerfile"


def text() -> str:
    return DOCKERFILE.read_text()


def problems(source: str, doc: str | None = None, root: Path | None = None) -> list[str]:
    """Run the gate's checks over `source` without touching the real files.

    Every branch here delegates to the gate. An earlier version reimplemented
    the copied-path loop instead, and an un-fix control found what that costs:
    deleting the check from `check_dockerfile` left this suite green, because
    the thing under test had quietly become the copy in this file.
    """
    captured: list[str] = []
    base, image = check_dockerfile._check_base(source)
    captured.extend(base)
    if image:
        captured.extend(check_dockerfile._check_python(image))
    captured.extend(check_dockerfile._check_copied(source))
    workdirs = [m.group("path") for m in check_dockerfile.WORKDIR.finditer(source)]
    captured.extend(check_dockerfile._check_entrypoint(source, workdirs[-1] if workdirs else None))
    if doc is not None:
        captured.append(doc)
    return captured


class TestTheBuildContext:
    """The claim that the copied paths are enough to build a wheel."""

    @staticmethod
    @pytest.fixture(scope="class")
    def built(tmp_path_factory: pytest.TempPathFactory) -> subprocess.CompletedProcess[str]:
        context = tmp_path_factory.mktemp("context")
        for source in check_dockerfile._copied(text()):
            origin = ROOT / source
            if origin.is_dir():
                shutil.copytree(origin, context / source)
            else:
                shutil.copy2(origin, context / source)
        return subprocess.run(
            [sys.executable, "-m", "pip", "wheel", "--no-deps", "-w", "dist", "."],
            cwd=context,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_a_wheel_builds_from_only_what_the_dockerfile_copies(
        self, built: subprocess.CompletedProcess[str]
    ) -> None:
        assert built.returncode == 0, (
            "the image's build stage copies too little to build a wheel; "
            f"pip said:\n{built.stdout[-2000:]}\n{built.stderr[-2000:]}"
        )

    def test_the_wheel_is_the_version_this_tree_declares(
        self, built: subprocess.CompletedProcess[str], tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """A wheel at the wrong version means the image would publish the wrong code."""
        from djaudit import __version__

        assert f"djaudit-{__version__}" in built.stdout, built.stdout[-2000:]


class TestTheEntrypoint:
    def test_the_entrypoint_is_djaudit_itself(self) -> None:
        """Not `djaudit run`, or naming a subcommand would append to it."""
        assert 'ENTRYPOINT ["djaudit"]' in text()

    def test_the_default_command_audits_the_working_directory(self) -> None:
        assert check_dockerfile.check() == 0

    def test_a_command_that_is_not_a_command_is_caught(self) -> None:
        broken = text().replace('CMD ["run", "/src"]', 'CMD ["scan", "/src"]')
        assert any("not a command" in p for p in problems(broken))

    def test_a_flag_the_command_rejects_is_caught(self) -> None:
        broken = text().replace('CMD ["run", "/src"]', 'CMD ["run", "/src", "--recursive"]')
        assert any("--recursive" in p for p in problems(broken))

    def test_auditing_a_directory_nobody_mounts_into_is_caught(self) -> None:
        """The quiet failure: djaudit exits 0 on an empty tree, so the image would pass."""
        broken = text().replace('CMD ["run", "/src"]', 'CMD ["run", "/app"]')
        assert any("WORKDIR" in p for p in problems(broken))

    def test_a_missing_entrypoint_is_caught(self) -> None:
        broken = text().replace('ENTRYPOINT ["djaudit"]', "")
        assert any("no ENTRYPOINT" in p for p in problems(broken))


class TestTheBaseImage:
    def test_the_base_is_pinned(self) -> None:
        broken = text().replace("python:3.13-slim", "python:latest")
        assert any("not pinned" in p for p in problems(broken))

    def test_a_python_below_requires_python_is_caught(self) -> None:
        """The image's interpreter is a promise the packaging metadata already made."""
        broken = text().replace("python:3.13-slim", "python:3.11-slim")
        assert any("below requires-python" in p for p in problems(broken))

    def test_stages_that_disagree_about_the_interpreter_are_caught(self) -> None:
        head, sep, tail = text().partition("FROM python:3.13-slim\n")
        assert sep, "the runtime stage's FROM moved; this test needs updating"
        assert any(
            "different bases" in p for p in problems(head + "FROM python:3.12-slim\n" + tail)
        )


class TestItDoesNotRunAsRoot:
    def test_the_image_drops_privileges(self) -> None:
        assert check_dockerfile._check_user(text()) == []

    def test_no_user_at_all_is_caught(self) -> None:
        broken = text().replace("USER djaudit", "")
        assert any("never drops privileges" in p for p in check_dockerfile._check_user(broken))

    def test_running_as_root_is_caught(self) -> None:
        broken = text().replace("USER djaudit", "USER root")
        assert any("runs as root" in p for p in check_dockerfile._check_user(broken))

    def test_dropping_privileges_before_the_install_is_caught(self) -> None:
        """An unprivileged install lands somewhere the entrypoint will not look."""
        source = text().replace("USER djaudit\n", "")
        source = source.replace(
            "COPY --from=build /wheels /wheels", "USER djaudit\nCOPY --from=build /wheels /wheels"
        )
        assert any("before the last" in p for p in check_dockerfile._check_user(source))

    def test_the_gate_actually_runs_this_check(self) -> None:
        """`_check_user` is only worth testing while `check` still calls it.

        Without this, deleting the call from `check` would leave every test in
        this class passing -- they would be exercising a function the gate no
        longer consults.
        """
        import inspect

        assert "_check_user" in inspect.getsource(check_dockerfile.check)


class TestTheBuildContextIsSmall:
    """`.dockerignore` is not cosmetic: the context is uploaded before any COPY."""

    def test_the_ignore_file_exists(self) -> None:
        assert (ROOT / ".dockerignore").exists()

    @pytest.mark.parametrize("path", ["benchmarks", "tests", ".git"])
    def test_the_heavy_directories_are_excluded(self, path: str) -> None:
        ignored = {
            line.strip()
            for line in (ROOT / ".dockerignore").read_text().splitlines()
            if line.strip() and not line.startswith("#")
        }
        assert path in ignored, f"{path} would be uploaded with every build"

    def test_nothing_the_build_needs_is_excluded(self) -> None:
        """The opposite failure, and the one that breaks the build outright."""
        ignored = {
            line.strip()
            for line in (ROOT / ".dockerignore").read_text().splitlines()
            if line.strip() and not line.startswith("#")
        }
        for needed in check_dockerfile._copied(text()):
            assert needed not in ignored, (
                f"the Dockerfile copies {needed}, but .dockerignore drops it"
            )


class TestTheDocumentedInterface:
    def test_the_doc_exists_and_names_the_mount(self) -> None:
        assert check_dockerfile._check_doc("/src") == []

    def test_a_doc_that_never_mentions_the_mount_is_caught(self, tmp_path: Path) -> None:
        assert any("never mentions" in p for p in check_dockerfile._check_doc("/elsewhere"))

    def test_the_doc_says_what_happens_to_the_live_tier(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The image holds djaudit alone, so `--live` can never work inside one.

        Driven through the gate against a doc with every mention stripped, not
        by reading the real file: asserting on the real text would pass whether
        or not the gate still looks, and an un-fix control proved it did.
        """
        stripped = tmp_path / "container.md"
        stripped.write_text((ROOT / "docs/container.md").read_text().replace("--live", "it"))
        monkeypatch.setattr(check_dockerfile, "DOC", stripped)

        assert any("live tier" in p for p in check_dockerfile._check_doc("/src"))

    def test_the_shipped_doc_still_covers_the_live_tier(self) -> None:
        """The contrast: the same gate passes on what we actually ship."""
        assert check_dockerfile._check_doc("/src") == []
        assert "--live" in (ROOT / "docs/container.md").read_text()

    def test_the_gate_actually_runs_the_doc_check(self) -> None:
        import inspect

        assert "_check_doc" in inspect.getsource(check_dockerfile.check)

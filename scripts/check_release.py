"""Check that a release can only happen the way we say it happens.

Publishing is the one action in this repository that cannot be taken back. A
version on PyPI is permanent, its file contents are immutable, and whatever
`__version__` says at build time is stamped into every JSON and SARIF report
that release will ever produce. So the claims worth checking here are the ones
whose failure is discovered by users:

* **The version has one source.** `pyproject.toml` used to carry a literal
  alongside `src/djaudit/__init__.py`, which is two answers to "which version
  produced this report" and no mechanism for keeping them equal.
* **The tag is the version.** Checked with `--tag` before anything is built,
  because a mismatch does not fail -- it succeeds, and publishes an artefact
  stamped with a version nobody tagged.
* **Publishing is gated.** The publish job must depend, transitively, on the
  whole CI workflow running on the tagged tree.
* **Publishing needs no long-lived credential.** Trusted publishing exchanges
  a short-lived OIDC token that PyPI accepts only from this workflow, in this
  repository, in this environment. An API token in a secret can publish from
  anywhere, forever, and its compromise is not visible from here.
* **A release cannot be started by hand.** No `workflow_dispatch`, no branch
  push: both are paths by which an untested commit reaches PyPI under a
  version that has no tag pointing at it.

None of these break a test when they change, which is the same argument the
documentation gates make. The difference is that this one guards an action
with no undo.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from djaudit import __version__

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
RELEASE = ROOT / ".github" / "workflows" / "release.yml"
CI = ROOT / ".github" / "workflows" / "ci.yml"

# PEP 440's release segment, which is what we actually intend to publish.
# Deliberately narrower than PEP 440 allows: an epoch or a local version in a
# tag is far more likely to be a typo than a decision.
VERSION = re.compile(r"^\d+\.\d+\.\d+(?:(?:a|b|rc)\d+|\.post\d+|\.dev\d+)?$")


def fail(message: str) -> None:
    print(f"release: {message}")


def triggers(workflow: Mapping[object, Any]) -> dict[str, Any]:
    """The `on:` block, which PyYAML reads as the boolean `True`.

    YAML 1.1 resolves the bare word `on` to true, so `workflow["on"]` is a
    `KeyError` on a file that plainly contains one. Reading it wrongly would
    make every trigger check below vacuous rather than failing, which is the
    shape of bug these gates exist to catch.
    """
    if True in workflow:
        found = workflow[True]
    elif "on" in workflow:
        found = workflow["on"]
    else:
        return {}
    return found if isinstance(found, dict) else dict.fromkeys(found, None)


def _version_source(problems: list[str]) -> None:
    """One literal, in the file the running code reads."""

    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    project = data.get("project", {})

    if "version" in project:
        problems.append(
            f"pyproject declares version {project['version']!r} as a literal, "
            f"so the package and djaudit.__version__ are two answers"
        )
    if "version" not in project.get("dynamic", []):
        problems.append("pyproject does not declare version as dynamic")

    path = data.get("tool", {}).get("hatch", {}).get("version", {}).get("path")
    if not path:
        problems.append("no [tool.hatch.version] path, so the build has no version to read")
        return

    source = ROOT / path
    if not source.is_file():
        problems.append(f"[tool.hatch.version] reads {path}, which does not exist")
        return

    found = re.search(r'^__version__ = "([^"]+)"', source.read_text(encoding="utf-8"), re.MULTILINE)
    if not found:
        problems.append(f"{path} defines no __version__ for the build to read")
    elif found.group(1) != __version__:
        problems.append(f"{path} says {found.group(1)}, the imported package says {__version__}")
    elif not VERSION.match(__version__):
        problems.append(f"{__version__} is not a release version we know how to tag")


def _release_workflow(problems: list[str]) -> None:
    """The publish path: what starts it, what gates it, what it proves."""

    if not RELEASE.is_file():
        problems.append("there is no release workflow")
        return

    workflow = yaml.safe_load(RELEASE.read_text(encoding="utf-8"))
    fired_by = triggers(workflow)
    if not fired_by:
        problems.append("the release workflow has no triggers, so it cannot be read")
        return

    for by_hand in ("workflow_dispatch", "schedule", "pull_request"):
        if by_hand in fired_by:
            problems.append(f"the release workflow can be started by {by_hand}")
    push = fired_by.get("push") or {}
    if "tags" not in push:
        problems.append("the release workflow does not trigger on tags")
    if "branches" in push:
        problems.append("the release workflow triggers on a branch push, not only on a tag")

    jobs = workflow.get("jobs", {})
    publishing = [
        name
        for name, job in jobs.items()
        if any("pypi-publish" in str(step.get("uses", "")) for step in job.get("steps", []))
    ]
    if not publishing:
        problems.append("no job publishes anything, so this workflow releases nothing")
        return
    if len(publishing) > 1:
        problems.append(f"{len(publishing)} jobs publish; there should be one")

    name = publishing[0]
    job = jobs[name]

    for step in job.get("steps", []):
        with_block = step.get("with") or {}
        for credential in ("password", "user", "api-token", "token"):
            if credential in with_block:
                problems.append(
                    f"the {name} job passes a {credential}; trusted publishing needs none, "
                    f"and a long-lived credential can publish from anywhere"
                )
    if "secrets" in str(job).lower() and "GITHUB_TOKEN" not in str(job):
        problems.append(f"the {name} job reads a secret, which trusted publishing does not need")

    permissions = job.get("permissions") or {}
    if permissions.get("id-token") != "write":
        problems.append(f"the {name} job cannot mint an OIDC token, so it cannot publish")
    if not job.get("environment"):
        problems.append(
            f"the {name} job declares no environment, which is what PyPI's "
            f"trusted publisher configuration pins"
        )

    _gated(jobs, name, problems)


def _gated(jobs: dict[str, Any], publisher: str, problems: list[str]) -> None:
    """Publishing must reach the whole CI workflow through `needs`."""

    def needs(name: str) -> list[str]:
        declared = jobs.get(name, {}).get("needs") or []
        return [declared] if isinstance(declared, str) else list(declared)

    seen: set[str] = set()
    queue = needs(publisher)
    if not queue:
        problems.append(f"the {publisher} job depends on nothing, so a tag publishes untested code")
        return
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        seen.add(name)
        queue.extend(needs(name))

    calls_ci = any(
        str(jobs[name].get("uses", "")).endswith(CI.name) for name in seen if name in jobs
    )
    if not calls_ci:
        problems.append(
            f"nothing {publisher} depends on runs {CI.name}, so the gate is not on the tag"
        )

    if not CI.is_file():
        problems.append(f"{CI.name} does not exist to be called")
        return
    if "workflow_call" not in triggers(yaml.safe_load(CI.read_text(encoding="utf-8"))):
        problems.append(f"{CI.name} is not callable, so the release cannot run it")


def _tag(tag: str, problems: list[str]) -> None:
    """The one check that runs during a release rather than about it."""

    if not tag.startswith("v"):
        problems.append(f"tag {tag} does not start with v")
        return
    if tag[1:] != __version__:
        problems.append(f"tag {tag} does not match the package version {__version__}")


def _typed(problems: list[str]) -> None:
    """The package must tell consumers it is typed, or none of the typing ships.

    Every gate in this repository reads the source tree, where the annotations
    are plainly visible, so all of them agreed the project was fully typed
    while the published wheel handed importers `Any` for the entire API. This
    is the one fact that can only be established from the built artefact's
    point of view: PEP 561 requires the marker file, and mypy ignores an
    installed package without it no matter how well annotated it is.
    """
    marker = ROOT / "src" / "djaudit" / "py.typed"
    if not marker.is_file():
        problems.append(
            "src/djaudit/py.typed is missing, so mypy treats the installed "
            "package as untyped and every import resolves to Any"
        )
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    if '"Typing :: Typed"' not in text:
        problems.append('pyproject.toml does not declare the "Typing :: Typed" classifier')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", help="the tag being released, e.g. v0.2.0")
    arguments = parser.parse_args(argv)

    problems: list[str] = []
    _version_source(problems)
    _release_workflow(problems)
    _typed(problems)
    if arguments.tag:
        _tag(arguments.tag, problems)

    for problem in problems:
        fail(problem)
    if problems:
        return 1

    if arguments.tag:
        print(f"release {arguments.tag} consistent: version {__version__}, gated, no token")
    else:
        print(f"release path consistent: version {__version__} from one source, gated, no token")
    return 0


if __name__ == "__main__":
    sys.exit(main())

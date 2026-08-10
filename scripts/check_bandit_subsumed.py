"""Check that bandit still has nothing to tell us that ruff does not.

Step 5.3.3 planned a bandit adapter and measurement declined it: ruff's `S`
rules are a port of bandit, and on 3,091 files of real Django code bandit's
only unshared output was noise or a duplicate. A decline is a decision that
stops being re-examined the moment it is written down, so the structural half
of it is checked here instead of trusted.

The structural claim is inventory, not counts: *every bandit check either has
a ruff counterpart or is one of four known exceptions.* That is cheap -- both
tools can list their own checks in well under a second, with no corpus to scan
-- and it is the half that can change without anyone noticing, because bandit
gains checks between releases. If bandit adds a Django-relevant one, this fails
and the decline gets read again.

The empirical half (which findings actually appeared, and which were wrong)
lives in `docs/PROJECT_PLAN.md`, because re-running it costs about a minute per
project and re-measuring on every commit would buy nothing.

This script fails rather than skips when a tool is missing. A gate that passes
because the thing it checks was absent is the failure this project has already
had once; bandit and ruff are both dev dependencies, so absence is a broken
environment and not a reason to say nothing.
"""

from __future__ import annotations

import json
import subprocess
import sys

# Every bandit check with no ruff `S` counterpart, and why it does not change
# the decision. Anything not on this list is new since the measurement.
EXCEPTIONS = {
    "B613": (
        "trojansource",
        "ruff has it as PLE2502 outside the S family, and it fires nowhere in the corpus",
    ),
    "B614": ("pytorch_load", "PyTorch, which a Django audit does not reach"),
    "B615": (
        "huggingface_unsafe_download",
        "HuggingFace, which a Django audit does not reach",
    ),
    "B703": (
        "django_mark_safe",
        "a strict duplicate of B308 -- all 101 corpus findings share a line "
        "with one, none stands alone -- and DJI-011 already makes the claim "
        "with provenance, which is what separates a real one from a helper",
    ),
}


def bandit_checks() -> dict[str, str]:
    """Every check bandit can run, by id, read from bandit itself.

    Its plugins and its blacklist are two separate registries and both produce
    findings, so reading only the first would understate what bandit does.
    """
    # Deferred so a missing bandit becomes the sentence below rather than a
    # traceback from the import line of a script whose job is to explain.
    from bandit.core import extension_loader  # noqa: PLC0415

    manager = extension_loader.MANAGER
    checks = {plugin.plugin._test_id: plugin.name for plugin in manager.plugins}
    for items in manager.blacklist.values():
        for item in items:
            checks[item["id"]] = item["name"]
    return checks


def ruff_security_rules() -> set[str]:
    completed = subprocess.run(
        ["ruff", "rule", "--all", "--output-format", "json"],
        capture_output=True,
        text=True,
        check=True,
    )
    return {rule["code"] for rule in json.loads(completed.stdout) if rule["code"].startswith("S")}


def main() -> int:
    try:
        checks = bandit_checks()
    except ImportError:
        print("bandit-subsumed: bandit is not installed, so nothing was checked")
        return 1
    try:
        ruff = ruff_security_rules()
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        print(f"bandit-subsumed: ruff could not list its rules ({exc}), so nothing was checked")
        return 1

    if len(checks) < 50 or len(ruff) < 50:
        # Both inventories were three figures when measured. A short one means
        # we are reading the wrong thing, and comparing two empty sets passes.
        print(f"bandit-subsumed: implausible inventory -- bandit {len(checks)}, ruff S {len(ruff)}")
        return 1

    unshared = {code for code in checks if "S" + code[1:] not in ruff}
    problems = []
    for code in sorted(unshared - set(EXCEPTIONS)):
        problems.append(
            f"bandit {code} ({checks[code]}) has no ruff counterpart and no recorded reason -- "
            f"decide whether it changes Step 5.3.3"
        )
    for code in sorted(set(EXCEPTIONS) - unshared):
        problems.append(
            f"{code} is listed as unshared but ruff now has S{code[1:]} -- drop it from EXCEPTIONS"
        )
    for code in sorted(unshared & set(EXCEPTIONS)):
        expected, _ = EXCEPTIONS[code]
        if checks[code] != expected:
            problems.append(f"{code} is now called {checks[code]}, not {expected}")

    for problem in problems:
        print(f"bandit-subsumed: {problem}")
    if problems:
        return 1

    print(
        f"bandit subsumed by ruff: {len(checks)} bandit checks · "
        f"{len(checks) - len(unshared)} with a ruff S counterpart · "
        f"{len(unshared)} exceptions, all accounted for"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

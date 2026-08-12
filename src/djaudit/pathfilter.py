"""Which findings a project has asked not to hear about, by location.

The design question was where exclusions apply, and it was settled by
measuring rather than by choosing. Dropping excluded files before parsing is
the obvious implementation and it is catastrophic: removing
``inventory/models.py`` from the ORM fixture -- one file, holding 2 of its 15
findings -- takes the whole run from 15 findings to **zero**, because every
``DJP`` finding in the views, the serializers and the management command is
derived from the model graph that file builds. An exclusion written that way
does not hide the file you named; it silently empties the audit and exits
clean.

So exclusions filter findings by location, after every rule has run and seen
the whole project. Excluding ``tests/`` means "do not tell me about findings in
tests", never "pretend those files do not exist".

Patterns are matched against the finding's path relative to the project root,
in posix form. A bare directory name excludes everything beneath it, because
``exclude_paths = ["migrations"]`` meaning only a file literally called
``migrations`` would be a trap.

The empty pattern is rejected by the CLI rather than skipped here. A guard in
this function could not fire -- against a *relative* path, ``""`` matches
nothing, ``"/*"`` matches nothing and ``startswith("/")`` is never true -- so
it would have been dead code that reads like a safety net. An empty pattern is
a mistake in the configuration, and the caller says so out loud.
"""

from __future__ import annotations

from fnmatch import fnmatchcase


def excluded(relative: str, patterns: tuple[str, ...]) -> bool:
    """Whether a project-relative posix path matches any pattern."""
    for pattern in patterns:
        bare = pattern.rstrip("/")
        if fnmatchcase(relative, pattern) or fnmatchcase(relative, f"{bare}/*"):
            return True
        # `*` in fnmatch crosses `/`, but a literal directory prefix still has
        # to work when the pattern names it without a trailing glob.
        if relative.startswith(f"{bare}/"):
            return True
    return False

# Configuration reference

djaudit reads `[tool.djaudit]` from the `pyproject.toml` at the root of the
project being analysed. Every setting has a command-line equivalent, and the
command line always wins.

## Precedence

A flag you typed beats the file. A flag you did not type does not.

That distinction is the whole of the precedence rule, and it is not the same as
"the flag is at its default value". `--min-severity` defaults to `medium`, so if
precedence compared values, a project stating `min_severity = "critical"` would
be silently overruled by a default nobody typed. djaudit asks click which
source each parameter came from, and only a value that came from the command
line displaces the file.

```
$ djaudit run .                          # file wins
$ djaudit run . --min-severity low       # flag wins, even if the file says low
```

Anything neither the flag nor the file states falls through to the built-in
default.

## Settings

All keys live under `[tool.djaudit]`. Unknown keys are an error, not a warning
— a setting that does nothing is worse than one that is refused, because you
cannot tell it apart from one that worked. A hyphenated key (`min-severity`)
is refused by name with the spelling it should have had.

| Key | Type | Default | Flag |
|---|---|---|---|
| `min_severity` | `"critical"`, `"high"`, `"medium"`, `"low"`, `"info"` | `"medium"` | `--min-severity` |
| `min_confidence` | `"certain"`, `"firm"`, `"tentative"` | `"firm"` | `--min-confidence` |
| `fail_on` | severity | `"medium"` | `--fail-on` |
| `family` | list of `"DJS"`, `"DJI"`, `"DJA"`, `"DJD"`, `"DJP"`, `"DJM"`, `"DJX"` | all | `--family` |
| `select` | list of rule ids | all | `--select` |
| `ignore` | list of rule ids | none | `--ignore` |
| `exclude_paths` | list of glob patterns | none | `--exclude-path` |
| `baseline` | path | none | `--baseline` |
| `format` | `"terminal"`, `"json"`, `"sarif"` | `"terminal"` | `--format` |
| `output` | path | stdout | `-o` / `--output` |

Rule ids are upper-cased on both paths, so `ignore = ["djp-001"]` works.

```toml
[tool.djaudit]
min_severity = "low"
fail_on = "high"
ignore = ["DJX-004"]
baseline = ".djaudit-baseline.json"
```

### `min_severity` and `fail_on` are different questions

`min_severity` decides what appears in the report. `fail_on` decides what makes
the command exit non-zero. Reporting everything and failing on `high` keeps the
smaller findings visible without blocking the build, which is usually what you
want in CI.

### Severity and confidence are separate axes

`min_confidence` filters on how sure djaudit is, not on how bad the thing is. A
`critical` finding at `tentative` confidence is "if this is what it looks like,
it is very bad" — usually a value that could not be resolved statically.
Filtering only on severity keeps those; filtering only on confidence keeps
`low` findings djaudit is certain about. Set both deliberately.

## Excluding paths

```toml
[tool.djaudit]
exclude_paths = ["tests", "*/migrations/*", "vendor/legacy.py"]
```

Patterns are matched against each finding's path relative to the project root,
in posix form, with `fnmatch`. A bare directory name excludes everything
beneath it.

**Exclusions hide findings; they do not skip files.** The distinction is not
academic. Every cross-file rule — the N+1 detector, the serializer analysis,
the portability rules — is built on a model graph assembled from your
`models.py` files. On djaudit's own ORM fixture, a full run reports 15 findings
across 5 files, of which `inventory/models.py` holds 2. Removing that file from
the analysis reports **zero**, because the graph the other 13 depend on is
gone. Excluding it by pattern reports 13, which is what you asked for.

So excluding `models.py` is safe, and excluding your entire application
directory is safe, and neither will quietly turn the rest of the audit off.

The count of what was hidden is reported — `suppressed_path` in the JSON
summary, and `N in excluded paths` on the terminal — because a run that hid 300
findings and a genuinely clean project would otherwise print the same thing.

An empty pattern is refused rather than ignored.

## Overriding severity

```toml
[tool.djaudit.severity]
"DJD-002" = "info"
"DJI-001" = "critical"
```

or, on the command line:

```
$ djaudit run . --severity DJD-002=info --severity DJI-001=critical
```

Overrides are applied **before** `--min-severity` and `--fail-on`, so they
change what is reported and what fails the build — not merely the word printed
next to a finding that was going to appear anyway. Raising a `low` rule to
`critical` brings it through a `--min-severity high` filter; lowering a `high`
rule drops it out.

An id no rule owns is refused, with a suggestion. A typo'd override is
otherwise a setting that does nothing forever, and looks exactly like one
correctly applied to a rule your project never triggers.

**Overrides do not move fingerprints.** A fingerprint is computed from the rule
id, the file and a normalised snippet; severity was never part of it. Re-ranking
a rule will not invalidate a baseline you have committed.

## What a configuration file may not do

`live = true` and `external = true` are refused. Both are available only as
command-line flags.

The reason is the threat model. djaudit's static tier never imports or executes
the code it analyses, which is what makes it safe to point at a repository you
have not read. `--live` runs `manage.py` inside the target's environment;
`--external` shells out to other tools. A `pyproject.toml` arrives *with* the
repository you were asked to analyse, so a configuration file that could turn
either on would let an untrusted repository arrange its own execution. Asking
for it has to be a decision made outside the repository.

```toml
[tool.djaudit]
live = true          # error: use --live
```

`--write-baseline` is likewise flag-only. Writing a baseline is a deliberate
act, and a file that could ask for it could ask a CI run to accept the very
findings it was supposed to report.

## The LLM subtable

`[tool.djaudit.llm]` configures the optional triage layer and is documented
separately in [architecture/llm-layer.md](architecture/llm-layer.md). It is read by the LLM layer, not by the audit,
and the audit never changes behaviour based on it.

## Checking your configuration

A malformed file fails the run with a message naming the file, the key and what
was wrong with it, rather than being ignored:

```
error: /srv/app/pyproject.toml: [tool.djaudit] min_severity must be one of
'critical', 'high', 'medium', 'low', 'info'
```

If a setting appears to do nothing, check whether you are also passing the
corresponding flag — the flag wins, silently and by design.

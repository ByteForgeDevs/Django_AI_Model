# The GitHub Action

`djaudit` ships a composite action that runs the audit, uploads the results to
GitHub code scanning, and fails the job on findings — in that order.

```yaml
name: audit
on: [push, pull_request]

jobs:
  djaudit:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      security-events: write
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.13"
      - uses: ByteForgeDevs/Django_AI_Model@v0.4.0
        with:
          path: .
```

`security-events: write` is required for the upload. Without it the upload
step fails even though the audit succeeded.

## Why the ordering matters

A composite action stops at its first failing step. If the audit failed the
step directly, the upload would never run, and the findings would be missing
from code scanning in exactly the case where they matter.

So the audit records its exit code instead of acting on it, the upload happens
next, and a final step fails the job. A crash is treated differently from a
finding: if the audit exits non-zero *without* writing a report, it failed to
run, and the action stops there rather than uploading nothing.

## Inputs

| Input | Default | What it does |
|---|---|---|
| `path` | `.` | Path to the Django project to audit. |
| `version` | the released version | Which djaudit to install, as a PEP 440 specifier. |
| `install` | `true` | Install djaudit first. Set `false` when the workflow already installed it. |
| `fail-on` | `high` | Severity at which the job fails: `critical`, `high`, `medium`, `low`, `info`. |
| `min-severity` | `low` | Hide findings below this severity. |
| `min-confidence` | `firm` | Hide findings below this confidence: `certain`, `firm`, `tentative`. |
| `baseline` | none | Baseline file whose findings are suppressed. |
| `args` | none | Extra arguments passed to `djaudit run` verbatim. Split on whitespace. |
| `sarif-file` | `djaudit.sarif` | Where the SARIF report is written. |
| `upload-sarif` | `true` | Upload to code scanning. |

## Outputs

| Output | What it is |
|---|---|
| `sarif-file` | Path to the report, written whether or not the audit passed. |
| `exit-code` | `0` if nothing reached `fail-on`, `1` if something did, anything else means djaudit failed to run. |

## Getting an HTML report as well

The action hardcodes `--format sarif`, because SARIF is what code scanning
consumes. **Do not try to change that through `args`.** The flag would be
passed twice and the last one wins, which means `--format html` in `args`
writes an HTML document into the file named `djaudit.sarif`. The action's own
"did it write a report" guard is satisfied — the file exists — so the run
proceeds to the upload step and fails there, or uploads something that is not
SARIF. Verified: passing `--format` twice produces the second format, silently.

Run a second step instead. It is a second audit, so it costs the runtime again,
but the two are independent and the HTML one cannot break the upload:

```yaml
      - uses: ByteForgeDevs/Django_AI_Model@v0.4.0

      - name: Render a readable report
        if: always()
        run: djaudit run . --format html -o djaudit-report.html || true

      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: djaudit-report
          path: djaudit-report.html
```

`install: true` on the first step already put djaudit on `PATH`, so the second
step does not reinstall it. `if: always()` on both, because the report is most
wanted on the run that failed, and `|| true` because `djaudit run` exits `1`
when it finds something and that must not fail the job twice.

See [report.md](report.md) for what the report contains.

## Adopting on an existing codebase

A mature codebase will not be clean on the first run, and a job that fails from
day one gets switched off. Record what is there today and fail only on what is
added after:

```yaml
      - uses: ByteForgeDevs/Django_AI_Model@v0.4.0
        with:
          baseline: .djaudit-baseline.json
```

Generate that file once with `djaudit run --write-baseline
.djaudit-baseline.json` and commit it. Findings are matched by fingerprint, so
they survive the code moving around them.

## What this does not do

- **It does not run the live tier.** Live rules execute the audited project's
  own `manage.py`, which needs its dependencies and its settings. Pass
  `args: --live` only if the workflow has installed them.
- **It does not upload from forks.** Code scanning uploads are unavailable to
  pull requests from forked repositories; the audit still runs and still fails
  the job, but the results will not appear in the Security tab.
- **`args` cannot carry an argument containing a space.** It is split on
  whitespace before reaching the CLI.

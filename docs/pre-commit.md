# The pre-commit hooks

`djaudit` publishes hooks for [pre-commit](https://pre-commit.com). Add this to
`.pre-commit-config.yaml`:

```yaml
repos:
  - repo: https://github.com/ByteForgeDevs/Django_AI_Model
    rev: v0.4.0
    hooks:
      - id: djaudit
```

Then `pre-commit install`. The audit runs on any commit that touches a Python
file and fails the commit on a finding of `high` severity or worse.

## The hooks

- id: djaudit

  The full audit — settings hardening, injection, DRF authorization, ORM
  performance, migration safety and portability.

- id: djaudit-security

  Restricted to `DJS`, `DJI` and `DJA`: the families that describe a way in.
  Useful when you want a fast signal locally and are happy to let CI run the
  rest.

## Why it audits the whole project

djaudit's unit of analysis is the project, not the file. It resolves your
settings modules, builds a model graph and a DRF route graph, and follows taint
between files. A rule like DJA-004 needs the viewset *and* the serializer *and*
the model, which will rarely be in the same commit.

So the hooks set `pass_filenames: false` and audit the repository root. This has
two consequences worth knowing:

- **The audit does not get smaller as your commit gets smaller.** It takes the
  same few seconds whether you changed one line or one hundred.
- **You cannot narrow it by pointing at a subdirectory.** An app package
  contains no settings module, so djaudit resolves no settings, reports
  nothing, and exits successfully. Use `--family` or `--select` to narrow the
  audit instead; those narrow the rules without blinding the analysis.

## Making it faster

A few seconds on every commit is more than some people want. Options, in
increasing order of how much they give up:

```yaml
      - id: djaudit-security          # fewer rules
      - id: djaudit
        args: [--fail-on, critical]   # same rules, fails less often
        stages: [pre-push]            # or: not on every commit
```

## Adopting on an existing codebase

Record what is already there, so the hook only fails on what you add:

```yaml
      - id: djaudit
        args: [--fail-on, high, --baseline, .djaudit-baseline.json]
```

Generate the baseline once with `djaudit run --write-baseline
.djaudit-baseline.json` and commit it. Findings are matched by fingerprint, so
they survive the surrounding code moving.

## What this does not do

- **It does not run the live tier.** Live rules execute your `manage.py`, which
  is not something a commit hook should do without being asked. Run
  `djaudit run --live` yourself, or in CI.
- **It does not run on non-Python commits.** The hooks declare
  `types: [python]`. A commit that changes only templates or configuration will
  not be audited, including a change to `[tool.djaudit]` in `pyproject.toml`.

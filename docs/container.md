# Running djaudit from a container

The image is for CI systems that will not install Python packages — a runner
that has docker and nothing else, a pipeline that pins tools by digest, a
security team that wants the analyzer isolated from the code it reads.

If you can `pip install djaudit`, install it. The image buys isolation and a
pinned interpreter; it costs you a mount, a bind of your source tree, and the
live tier.

## Getting it

```console
$ docker pull ghcr.io/byteforgedevs/djaudit:0.4.0
```

Tags follow the release: `0.4.0` is the version in `pyproject.toml`, and the
image at that tag was built from that tag's source. Nothing is installed from
PyPI at build time, so an image and the code it audits with cannot drift.

## Running it

Mount the project at `/src` and run:

```console
$ docker run --rm -v "$PWD:/src:ro" ghcr.io/byteforgedevs/djaudit:0.4.0
```

That is the whole interface. `/src` is the working directory and the default
argument, so a bare `docker run` audits whatever you mounted.

The mount is read-only because djaudit does not write to the project it reads
and, in the static tier, does not execute it either. Keep the `:ro`.

### Subcommands

The entrypoint is `djaudit`, not `djaudit run`, so every subcommand is
reachable by naming it:

```console
$ docker run --rm ghcr.io/byteforgedevs/djaudit:0.4.0 rules
$ docker run --rm ghcr.io/byteforgedevs/djaudit:0.4.0 explain DJP-001
$ docker run --rm -v "$PWD:/src:ro" ghcr.io/byteforgedevs/djaudit:0.4.0 run /src --family DJS
```

Note the `/src` in the last one: naming a subcommand replaces `CMD` entirely,
including its path argument, so you have to supply the path yourself.

### Getting the report out

Every reporter writes to stdout when `--output` is absent, which is the
simplest way across a container boundary:

```console
$ docker run --rm -v "$PWD:/src:ro" ghcr.io/byteforgedevs/djaudit:0.4.0 \
    run /src --format sarif > djaudit.sarif
```

If you would rather have djaudit write the file, mount somewhere writable —
`/src` is read-only and the container runs as uid 1000:

```console
$ docker run --rm -v "$PWD:/src:ro" -v "$PWD/out:/out" \
    ghcr.io/byteforgedevs/djaudit:0.4.0 run /src --format sarif -o /out/djaudit.sarif
```

SARIF locations are recorded relative to the analyzed root, with the absolute
path confined to `originalUriBaseIds`. A report produced at `/src` therefore
uploads to code scanning with the same paths as one produced on the host — the
container's directory layout does not leak into the annotations.

### Exit codes

Unchanged from the CLI, and they are the container's exit code:

| Code | Meaning |
|---|---|
| `0` | No findings at or above the thresholds |
| `1` | Findings were reported |
| `2` | djaudit could not run — bad path, bad flag |

A `2` is worth distinguishing in CI. `1` means the audit worked and found
something; `2` means it never got that far.

## What does not work in the image

**The live tier.** `--live` runs `manage.py check --deploy`, `sqlmigrate` and
database introspection inside the target's virtualenv, which means importing
the target's settings, its apps, and its dependencies. The image contains
djaudit and nothing else, so those imports fail. `--live` on a mounted project
will degrade to the static tier and say so in the diagnostics.

If you need the live tier, install djaudit into the environment that already
has the project's dependencies. That is what the live tier is: a tool running
next to the code, not over it.

**Writing to the project.** `--write-baseline` needs a writable path. Point it
at a mounted writable directory, not at `/src`.

## What is in the image

- `python:3.13-slim`, pinned.
- djaudit and its two runtime dependencies, `typer` and `rich`.
- A non-root user, `djaudit`, uid 1000.

Not present: git, a compiler, Django, or the target's dependencies. The static
tier parses source with the standard library's `ast` and needs none of them.

## Building it yourself

```console
$ docker build -t djaudit:dev .
$ docker run --rm -v "$PWD:/src:ro" djaudit:dev
```

The build is two stages. The first builds a wheel from `pyproject.toml`,
`README.md` and `src/`; the second installs that wheel and discards the
toolchain. The build context is deliberately small — `.dockerignore` keeps the
benchmark corpus, the test fixtures and the git history out of it.

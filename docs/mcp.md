# Using djaudit from a coding agent

`djaudit mcp` serves djaudit to an AI coding agent over the
[Model Context Protocol](https://modelcontextprotocol.io). The agent writes
Django code, calls the audit tool, sees what is wrong with a file and a line,
and repairs it before you ever see the diff.

This page is about that setup. If you want to run djaudit yourself, start with
[getting-started.md](getting-started.md).

## Why this exists

A language model writing Django is a plausible-continuation engine, and

```python
cursor.execute("SELECT * FROM orders WHERE id = " + request.GET["id"])
```

is an extremely plausible continuation. It is also a critical SQL injection.

The problem is not that models write bad Django — most of what they write is
fine. The problem is that asking a model to review its own output uses the same
faculty that produced it, so the same blind spot applies twice. Reading the line
above back produces no suspicion. Running a parser over it produces `DJI-001` in
under a second.

An MCP server is how you give a model a second opinion it did not generate.
Nothing here is generative: djaudit parses, matches 87 rules, and reports. The
agent does the writing, and the repairing.

## Setup

The server is launched by the client, over stdin/stdout. You never run it
yourself.

Install djaudit so it has a stable path. There is no published release yet, so
this is from a checkout:

```bash
uv tool install .
```

Then confirm the command exists — the most common setup failure is a client
pointed at an older installed copy:

```bash
djaudit mcp --help
```

### GitHub Copilot CLI

Add to `~/.copilot/mcp-config.json`:

```json
{
  "mcpServers": {
    "djaudit": {
      "type": "local",
      "command": "djaudit",
      "args": ["mcp"],
      "tools": ["*"]
    }
  }
}
```

### Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "djaudit": {
      "command": "djaudit",
      "args": ["mcp"]
    }
  }
}
```

### Cursor, Windsurf, Zed and others

The same two fields — a command and its arguments — under whatever key the
client uses for local MCP servers.

If your client cannot find `djaudit` on `PATH`, give the absolute path that
`which djaudit` prints. Clients are frequently launched from a desktop session
that does not share your shell's `PATH`.

## What the agent gets

Three tools.

### `audit_django_project`

Audits a project and returns its findings, most severe first.

| Argument | Required | Meaning |
|---|---|---|
| `path` | yes | Directory containing `manage.py` |
| `min_severity` | no | `critical`, `high`, `medium`, `low` or `info`. Default `low` |
| `families` | no | Any of `DJS`, `DJI`, `DJA`, `DJP`, `DJM`, `DJX`, `DJD` |

Each finding comes back with its rule id, severity, `file:line`, fingerprint,
message and **remediation**. The remediation is the point: a count of problems
is a notification, but a fix and a location are something the agent can act on.

The defaults match `djaudit run`, so a run you do by hand afterwards shows the
same findings the agent saw.

If djaudit could not fully read the project, the reply begins with
`ANALYSIS INCOMPLETE` and lists why. A short audit that reads as a clean one is
the failure mode this tool exists to avoid, and it is more dangerous here than
at a terminal, because nobody is watching.

### `explain_django_finding`

Takes a `path` and a `fingerprint` from an audit, and returns the long-form
explanation — what is wrong, why it matters, how to fix it, and what the rule
does not cover. The same text as [`djaudit explain`](getting-started.md).

### `list_django_rules`

The catalogue, optionally filtered by `family`. Useful when the agent is
deciding what to check rather than reacting to a finding.

## The loop

During the handshake the server sends an `instructions` string, which most
clients add to the model's system prompt. It asks for this:

1. Write the Django code.
2. Call `audit_django_project` on the project.
3. Fix what comes back, starting with the most severe.
4. Call it again to confirm the findings are gone.

Step 4 is not ceremony. A repair that is not re-audited is a repair that is
believed rather than checked, and repairs sometimes introduce new findings.

The instructions also tell the agent **not to suppress a finding to make the
audit pass**, because that is the shortest path to a green result and it is
always the wrong one. `# djaudit: ignore` and baseline entries are for humans
who have made a decision, not for an agent clearing its own report.

## What it does not do

- **It does not execute your code.** djaudit parses. It never imports the
  project, never runs `manage.py`, never evaluates settings. This matters more
  over MCP than anywhere else, because the server is launched automatically by
  an agent rather than by a person who chose to run it.
- **It does not reach the network.** No API keys, no telemetry, no model calls.
  The intelligence is in the client; djaudit is the part that is certain.
- **It does not use the live tier.** Live-tier rules run inside the target's
  virtualenv and are therefore opt-in at the command line only.
- **It does not write files.** Every tool is read-only. The agent applies its
  own edits, which means you review them the way you review any other diff.

## Checking that it works

Point a client at it and ask for an audit of a project you know has problems. A
bare `django-admin startproject` is a good test: it ships with
`SECRET_KEY = "django-insecure-..."`, `DEBUG = True` and no secure-cookie
settings, so a working server returns four findings and a broken one returns
nothing.

To see the same findings without a client in the way:

```bash
djaudit run /path/to/project
```

If that prints findings and the agent reports none, the client is running a
different djaudit than your shell is. Check the path in its config.

## See also

- [getting-started.md](getting-started.md) — running djaudit yourself
- [rules/README.md](rules/README.md) — every rule, by id
- [configuration.md](configuration.md) — `[tool.djaudit]` in `pyproject.toml`
- [authoring-rules.md](authoring-rules.md) — adding a rule of your own

# Versioning policy

`djaudit` publishes three version numbers. They are not decoration and they do
not move together, because they answer different questions for different
readers.

| Number | Where | Governs | Shape |
|---|---|---|---|
| `__version__` | `src/djaudit/__init__.py` | the released package | SemVer |
| `SCHEMA_VERSION` | `src/djaudit/models.py` | the JSON report contract | integer |
| `FINGERPRINT_VERSION` | `src/djaudit/fingerprint.py` | finding identity, and therefore every baseline | opaque string |

`scripts/gen_schema.py --check` enforces the mechanical parts of what follows
against `schema/contract.json`, on every CI run.

## `__version__` — the release

Standard SemVer, with the pre-1.0 caveat spelled out because it is routinely
got wrong: below `1.0.0` SemVer offers no compatibility promise on the major,
so the **minor** carries breakage. `0.1.x → 0.2.0` is this project's breaking
bump today; after `1.0.0` it becomes `1.x → 2.0.0`.

A release is breaking if it bumps `SCHEMA_VERSION` or `FINGERPRINT_VERSION`,
removes a rule id, or changes a CLI flag's meaning. Adding a rule is **not**
breaking, even though it can turn a passing build red — a new finding is the
tool working. Teams that need to adopt rules on their own schedule have the
baseline file for exactly that.

## `SCHEMA_VERSION` — the report

The JSON report is parsed by people we cannot contact. Breaking, therefore:

- removing or renaming a field,
- changing a field's type,
- adding a **new value to an enum**. Five are frozen member by member:
  `Severity` (`severity`), `Confidence` (`confidence`), `Tier` (`tier`),
  `Family` (`family`) and `EvidenceKind` (evidence `kind`). Their **order** is
  contract too, since a consumer may index a severity ladder rather than match
  on it. This is the change easiest to wave through, and is the reason
  the enums are recorded member by member: a consumer that dispatches on
  `severity` and falls through on the unknown will drop the finding, and
  dropping a `critical` because it was spelled in a way the reader predates is
  the worst failure this tool has available.

Adding a new **field** is not breaking. A reader that ignores unknown keys is
unaffected, and one that does not was going to break on anything.

A published shape is frozen. `schema/contract.json` keeps one entry per
`SCHEMA_VERSION`, and the gate re-derives the shape from the code and requires
it to match the entry exactly. There are two ways to satisfy it — revert, or
add an entry under a new version — and rewriting the old entry is not one,
because the gate also compares every published entry against the copy in
`HEAD`.

The contract is derived, never hand-written. It comes from running the reporter
over two specimens, one with every optional populated and one with everything
at its default, and merging what they emit. Both are needed: a single populated
specimen records `end_line` as an integer, when on a real run it is usually
`null`, and the contract would then promise consumers a field that is always
present. `tests/test_schema_contract.py` closes the loop by running djaudit on
real fixture projects and asserting the output conforms — a specimen is only
evidence for as long as it still resembles the thing it stands for.

Keys that are data rather than field names — `parse_errors`, `rule_errors`,
`findings[].properties` — collapse to `map`. Recording them would put one
specimen's filenames in the contract and demand a version bump the day the
specimen changed. `summary.by_severity` is deliberately *not* collapsed: its
keys are the `Severity` enum, so they are contract.

## `FINGERPRINT_VERSION` — identity, and every baseline downstream

This is the one with no visible symptom, and it is why the ledger exists.

A fingerprint is what makes two runs agree that they are looking at the same
problem. It is the key in every `.djaudit-baseline.json` committed in every
repository that has adopted the tool. Change how a snippet is normalised — a
`.lower()`, a different whitespace rule, a new field in the payload — and no
field name changes, no shape changes, and every test asserting that
fingerprints are *stable* still passes, because they are perfectly stable, at
new values. What happens instead is that every baseline entry everywhere stops
matching, and findings accepted months ago reappear as new. A team's first
experience of the upgrade is several hundred alerts they already triaged.

So `schema/contract.json` pins digests for a fixed set of inputs, chosen to
cover the parts that could drift unnoticed: whitespace collapsing, the
occurrence index, an empty snippet, and non-ASCII text. If the algorithm
changes, they change, and the gate says so.

Changing the algorithm is legitimate — but it means a new
`FINGERPRINT_VERSION`, a breaking release, and a note in the changelog telling
people their baseline needs regenerating.

## What this does not cover

Stated plainly, because a gate people over-trust is worse than no gate.

- **Meaning.** Nothing here notices if `DJS-001` quietly starts flagging
  something different under the same id and severity. The shape is identical
  and the fingerprint is identical; only the corpus benchmark and its triage
  file would catch it.
- **SARIF.** The SARIF reporter is versioned by the SARIF standard, not by us.
  `scripts/validate_sarif.py` checks it against the schema; `SCHEMA_VERSION`
  does not describe it.
- **The terminal reporter.** Human-readable output is deliberately not a
  contract. Parse the JSON.
- **Python and Django support windows.** Dropping a version is breaking and
  governed by `__version__` alone; there is no gate for it.

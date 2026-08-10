# External tool adapters

djaudit runs other people's tools and takes some of what they say. This note is
the decision record for *which* parts, and for why the default is to run none
of them.

Adapters are off unless `djaudit run --external` is given. The default is
load-bearing twice. Every precision number this project records was measured
without them, so a flag that leaked findings into a default run would
invalidate the benchmarks, the triage priors and the recorded subsumption
shortfall at once. And one adapter queries a vulnerability database over the
network, which is not something a static analyser should do because somebody
typed its name.

## The tools

**2 adapters** ship, in `src/djaudit/adapters/`, and `adapters.every()` returns
them in the order a run uses them:

| tool | what it is for | reaches the network |
|---|---|---|
| `ruff` | Django and security lint rules in Python source | no |
| `pip-audit` | known vulnerabilities in pinned dependencies | **yes** |

ruff is first because it is local, fast and always safe; `pip-audit` is last so
that a caller who stops a run partway through still gets the tool that costs
nothing. Which adapters reach a remote service is data, not prose:
`adapters.REACHES_THE_NETWORK` names them, and the CLI's disclosure is built
from it so that adding a third adapter that phones home cannot leave the
message describing the old set.

The disclosure is printed **before** the tools run, in `--live`'s spirit. A
notice that a vulnerability database was queried is worth nothing once the
query has been made.

## Nothing arrives unclaimed

An external tool's output is not evidence until somebody has decided what it
means to us. Each adapter carries a `ClaimTable`, and every rule code the tool
can emit has one of three verdicts:

- `ADOPT` — we report it, under a rule id of our own, with our own family,
  severity, confidence and remediation.
- `SUBSUMED` — one of our rules already covers it, named explicitly.
- `REJECTED` — we have looked and decided not to report it, with the reason
  written down.

A code with no entry is not silently dropped: it is reported as a diagnostic
naming the codes, because an undecided code is a decision we have not made
rather than a tool failure.

ruff's table holds **25 claims**: 4 adopted, 1 subsumed, 20 rejected. It is
selected with `--select DJ,S`, and on the three-project corpus that selection
produces 13,388 findings of which the table keeps **41**. The rejection rate is
the point of the layer. `pip-audit`'s table holds **1 claim**, because every
advisory it emits is the same kind of fact.

### The subsumption claim is measured, not asserted

`SUBSUMED` is the one verdict that deletes information. ruff's single
subsumption — `DJ001` covered by `DJD-002` — is checked by
`scripts/check_subsumption.py`, which runs ruff exactly as the adapter does and
asks, location by location, where the subsumed tool would have reported and we
do not. The answers are recorded in `benchmarks/subsumption/` and re-checked in
CI:

| target | `DJ001` locations | covered by `DJD-002` |
|---|---|---|
| healthchecks | 3 | 0 |
| netbox | 48 | 0 |
| pretix | 128 | 24 |

The shortfall is not a bug and is not zero. 179 locations are reported and 24
are covered; of the 155 that remain, 148 are exempt because the field is
`blank=True` and 2 under uniqueness — both documented `limitations` of `DJD-002` — and 5 sit
in a model that inherits from `django_otp`'s `Device` rather than
`models.Model` and is therefore absent from our model graph entirely. Two of
those five are genuine `DJD-002` material this subsumption silently deletes.
The gate exists so the number cannot drift without somebody re-reading it.

## Merging

`src/djaudit/adapters/merge.py` combines our findings with each report **by
fingerprint, not by location**, because location-based deduplication would be
wrong here. Measured across the corpus: our 268 findings and the external 41
share **zero** file-and-line collisions, while 9 locations in our own output
carry more than one finding and every one of them is two genuinely different
problems. Deduplicating by location would have deleted a real finding at each.

Adapters do not assign fingerprints — `Finding.fingerprint` is empty until
`merge.identified()` assigns one — so `identified()` assigns per group: ours,
then each report separately. A tool reporting the same text twice keeps two
findings; two tools reporting one thing keep one. Anything already fingerprinted
is returned untouched, because re-deriving over a different set can change an
occurrence index and silently invalidate baseline entries.

## Under our own rules

External findings are folded into a run inside the engine, at exactly one
point: *after* our own findings are fingerprinted, and *before* the baseline and
the thresholds. That position is deliberate. Folding later — the obvious place,
since adapters are a CLI concern — would mean `--external` bypassed
`--min-severity`, `--min-confidence`, `--baseline` and `# djaudit: ignore`,
turning the flag into a way to defeat the user's own filters.

So `# djaudit: ignore[RUFF-S324]` works on a linter's finding exactly as it
works on one of ours, and an adopted finding that does not clear the thresholds
is reported as filtered rather than as absent.

## Failure is ordinary

A missing tool is a fact about a machine, not an error in an audit. `collect`
never raises for anything its tool does: not installed, installed but not
executable, hanging, exiting non-zero, or emitting output we cannot parse all
come back as a `Report` that says so. An adapter object that raises anyway is
caught and recorded under the tool's name, under the same isolation rule as a
rule that crashes, and the other adapters are still asked.

Every tool asked for is named on stderr whether or not it found anything,
because a report that is short because a linter was missing looks exactly like a
report that is short because a project is clean. Notices and diagnostics never
go to stdout, which may be JSON or SARIF.

## Two things worth knowing about the tools themselves

**ruff and `pip-audit` both write to a file rather than a pipe.** Both can
produce more than the 1 MiB output cap the process runner enforces — ruff did,
on the first corpus run — so each is given an output path and the file is read
back.

**`--no-deps` is not what stops `pip-audit` resolving.** It only *permits*
`disable_pip`; `--disable-pip` is the flag that stops it. Without it the tool
builds a virtualenv, runs `pip install --dry-run`, reaches the network and
executes sdist build backends from the audited project. Frozen this way it
audits only direct exact pins, and it refuses a whole file if one requirement is
unpinned, so the adapter selects the pinned lines itself and reports the rest as
a diagnostic. `--vulnerability-service osv` is chosen over the default because
the default timed out at 82s where OSV answered in 21s; OSV returns duplicate
advisory entries, so results are deduplicated by `(package, id)`.

## bandit was declined

Not every adapter is worth writing, and the reasoning is checked rather than
remembered. bandit ships **75 checks**, **71** of which have a ruff `S`
counterpart we already run. Of the 4 exceptions, one (`B703`) is a strict
duplicate of a check bandit itself also ships, and one (`B113`) is wrong where
ruff is right. `scripts/check_bandit_subsumed.py` re-derives those numbers from
both tools' own inventories in CI, so the decision cannot quietly become false
when either tool ships a release.

## Where things are

| file | what it holds |
|---|---|
| `src/djaudit/adapters/base.py` | `Claim`, `ClaimTable`, `Report`, the `Adapter` protocol |
| `src/djaudit/adapters/process.py` | probing, running and version-reading, none of which raise |
| `src/djaudit/adapters/ruff.py` | the ruff adapter and its 25 claims |
| `src/djaudit/adapters/pip_audit.py` | the pip-audit adapter |
| `src/djaudit/adapters/merge.py` | fingerprint assignment and the merge |
| `scripts/check_subsumption.py` | the gate on the one `SUBSUMED` claim |
| `scripts/check_bandit_subsumed.py` | the gate on the declined adapter |
| `scripts/check_adapters_doc.py` | the gate on this file |

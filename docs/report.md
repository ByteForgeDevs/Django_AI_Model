# The HTML report

`--format html` writes one self-contained file. Open it with a double-click.

```console
$ djaudit run . --format html --output report.html
```

There is no server to start, no build step, and nothing is fetched when the
file opens — no CDN, no webfont, no analytics. It works on a machine with no
network, and nobody learns that you read it. Send it to someone who does not
have a checkout and it still renders.

Without `--output` the document goes to stdout, so it pipes like the other
formats.

## What it shows

Findings grouped by severity, each with its file and line, the source excerpt,
why it matters, and the fix. The filters along the top narrow by family,
severity and confidence. They run in the page, so filtering never re-runs the
audit and the counts never change under you.

The exit code is unchanged: `0` when nothing at or above the threshold was
found, `1` when something was. `--format html` is a rendering choice, not a
policy one.

## The same numbers as `--format json`

The report is rendered from the payload `--format json` emits, not rebuilt
alongside it. A report that recomputed its own counts would eventually
contradict the JSON your CI gates on, and the reader would have no way to tell
which one was right.

The payload is embedded in the file. To check the report against the pipeline
that produced it, or to re-process it later:

```console
$ python -c "import re,sys,json; print(json.loads(re.search(r'id=\"djaudit-payload\">(.*?)</script>', open(sys.argv[1]).read(), re.S).group(1))['summary'])" report.html
```

## What it admits

A prettier report that drops the caveats is worse than plain text, because it
reads as more authoritative while saying less. So the report surfaces:

- **findings below the threshold** — a count, so you know the list is filtered
- **degraded analysis** — rules that could not run in full
- **diagnostics** — anything that stopped djaudit reading part of the project
- **rule errors** — rules that crashed, named, with the error
- **parse errors** — files that could not be parsed, so were never analysed

An empty report says that nothing was found at or above the threshold. It does
not say the project is clean, because that is not what was measured.

## Opening a report about code you do not trust

djaudit parses the target and never imports or executes it, which is what makes
it safe to audit an unfamiliar repository. The HTML report is the one place
that guarantee could be handed away, because it embeds excerpts of the audited
source into a document you then open in a browser. A project containing
`</script><script>…</script>` in a string literal would otherwise get its code
run by whoever read the report about it — and the finding an attacker most
wants unread is the one describing their own code.

Two structural defences, not one habit:

- every value reaching the document is escaped with `html.escape(quote=True)`,
  and the embedded JSON escapes `<`, `>` and `&` to their `\u` forms, so no
  string in the data can terminate the element holding it
- the JavaScript never builds DOM from data. Filtering toggles `hidden` on
  elements already in the document. There is no `innerHTML` anywhere, so there
  is nothing for an escaped string to be un-escaped into

Both are tested against findings carrying script-breakout, attribute-breakout
and bare-tag payloads in every field that reaches the page. The tests parse the
result with `html.parser` and count elements rather than matching substrings,
and each defence has a control that disables it and requires the test to fail —
see `tests/test_html_report.py`.

Reference URLs render as text rather than links, so a rule that one day
interpolates project-supplied text into a reference cannot produce a clickable
`javascript:` target.

One thing the report does not do is re-read the audited files. Snippets come
from the finding, which has already been secret-masked, so a report about a
settings module does not put the key back on the page.

## Related

- [`getting-started.md`](getting-started.md) — installing and the first run
- [`configuration.md`](configuration.md) — thresholds, exclusions and severity
  overrides, all of which the report reflects
- [`github-action.md`](github-action.md) — SARIF, for findings in the GitHub UI
  rather than a file

"""A self-contained HTML report.

Everything the terminal prints, in a file you can open by double-clicking it.
The audience is the person who has to act on a finding and does not have a
terminal: a tech lead, a security reviewer, someone reading the artefact a CI
job uploaded.

Two properties make it worth having rather than a nicer-looking summary.

**It is a view of the JSON payload, not a second opinion.** Every number and
every string comes from :func:`json_reporter.build`, so the HTML and the JSON
cannot disagree about what the run found. A format that re-derives its own
counts is a format that will eventually contradict the one CI gates on.

**It shows what the run could not check.** `below threshold`, the degraded
block, blocking diagnostics, rule errors and parse errors are all on the page.
A report that renders only the findings is a prettier way to be misled: a clean
page from a run that read nothing looks exactly like a clean page from a run
that read everything, which is the confusion `djaudit.degradation` exists to
prevent.

Security note, because this file is opened in a browser and its content is
attacker-controlled: the report embeds source excerpts from the audited
project, and that project may be hostile -- auditing an untrusted repository is
a supported use. Two rules keep that from becoming script execution in the
reader's browser.

1. Every interpolated value goes through :func:`_esc`. There is no path from a
   payload string to the document that skips it.
2. The JavaScript never builds DOM from data. It sets ``hidden`` on rows that
   are already in the document, and reads only ``data-`` attributes it wrote
   itself. There is no ``innerHTML`` assignment anywhere in it, so there is
   nothing for an escaped string to break back out of.

The report is also offline by construction: no CDN, no font, no analytics, no
network of any kind. A report that phones home is a report that tells someone
else which project you audited.
"""

from __future__ import annotations

import html
import json
from collections.abc import Mapping
from typing import Any

from djaudit.engine import RunResult
from djaudit.provenance import Verdict
from djaudit.reporters import json_reporter

_SEVERITIES = ("critical", "high", "medium", "low", "info")
_CONFIDENCES = ("certain", "firm", "tentative")

_FAMILY_LABELS = {
    "DJS": "Settings & deployment",
    "DJI": "Injection & untrusted input",
    "DJA": "API authorization & exposure",
    "DJP": "Performance & ORM efficiency",
    "DJM": "Migration safety",
    "DJX": "Database portability",
    "DJD": "Data model design",
}


def _esc(value: object) -> str:
    """The only way a payload value is allowed to reach the document.

    ``quote=True`` matters: values are interpolated into attributes as well as
    into text, and an unescaped double quote in an attribute is a way out of it.
    """
    return html.escape(str(value), quote=True)


def _embed(payload: Mapping[str, Any]) -> str:
    """The payload, escaped so it cannot close the script element holding it.

    ``</script>`` anywhere in a string value would end the block and drop the
    remainder of the JSON into the document as markup. Escaping the three
    characters that could begin a tag is enough, and stays valid JSON: outside
    string literals JSON contains none of them, and inside one ``\\u003c`` is
    just how you spell ``<``.
    """
    text = json.dumps(dict(payload), indent=2)
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _badge(kind: str, value: str) -> str:
    return f'<span class="badge {kind}-{_esc(value)}">{_esc(value)}</span>'


def _evidence(items: list[dict[str, Any]]) -> str:
    if not items:
        return ""
    rows = []
    for item in items:
        source = item.get("source") or item.get("kind", "")
        rows.append(
            f'<figure class="ev"><figcaption>{_esc(source)}</figcaption>'
            f"<pre>{_esc(item.get('content', ''))}</pre></figure>"
        )
    return f'<div class="block"><h4>Evidence</h4>{"".join(rows)}</div>'


def _references(refs: list[str]) -> str:
    """References render as text, never as links.

    They come from the rule rather than from the audited project, so a link
    would be safe today. It would stop being safe the moment a rule interpolated
    a project-supplied string into one, and a reader cannot tell the two apart.
    """
    if not refs:
        return ""
    items = "".join(f"<li><code>{_esc(r)}</code></li>" for r in refs)
    return f'<div class="block"><h4>References</h4><ul class="refs">{items}</ul></div>'


def _prose(label: str, text: str) -> str:
    if not text:
        return ""
    return f'<div class="block"><h4>{_esc(label)}</h4><p>{_esc(text)}</p></div>'


def _finding(finding: dict[str, Any], index: int) -> str:
    loc = finding.get("location", {})
    where = f"{loc.get('file', '?')}:{loc.get('line', 0)}"
    family = str(finding.get("family", ""))
    # One lowercased haystack per finding so the search box never has to walk
    # the DOM. It is an attribute value, so it is escaped like everything else.
    haystack = " ".join(
        str(part).lower()
        for part in (
            finding.get("rule_id", ""),
            finding.get("title", ""),
            finding.get("message", ""),
            where,
            finding.get("fingerprint", ""),
        )
    )
    snippet = loc.get("snippet") or ""
    snippet_html = f'<pre class="snippet">{_esc(snippet)}</pre>' if snippet else ""

    triage = finding.get("triage")
    triage_html = ""
    if isinstance(triage, dict) and triage:
        pairs = "".join(f"<dt>{_esc(k)}</dt><dd>{_esc(v)}</dd>" for k, v in sorted(triage.items()))
        triage_html = f'<div class="block"><h4>Triage</h4><dl class="kv">{pairs}</dl></div>'

    return f"""<article class="finding" id="f{index}"
    data-severity="{_esc(finding.get("severity", ""))}"
    data-confidence="{_esc(finding.get("confidence", ""))}"
    data-family="{_esc(family)}"
    data-text="{_esc(haystack)}">
  <details>
    <summary>
      <span class="sev sev-{_esc(finding.get("severity", ""))}"></span>
      <code class="rid">{_esc(finding.get("rule_id", ""))}</code>
      <span class="ftitle">{_esc(finding.get("title", ""))}</span>
      <span class="where"><code>{_esc(where)}</code></span>
    </summary>
    <div class="body">
      <p class="msg">{_esc(finding.get("message", ""))}</p>
      {snippet_html}
      <div class="meta">
        {_badge("sev", str(finding.get("severity", "")))}
        {_badge("conf", str(finding.get("confidence", "")))}
        {_badge("tier", str(finding.get("tier", "")))}
        <span class="badge fam">{_esc(_FAMILY_LABELS.get(family, family))}</span>
        <span class="fp">fingerprint <code>{_esc(finding.get("fingerprint", ""))}</code></span>
      </div>
      {_prose("Why this matters", str(finding.get("rationale", "")))}
      {_prose("How to fix it", str(finding.get("remediation", "")))}
      {_evidence(list(finding.get("evidence", [])))}
      {_references([str(r) for r in finding.get("references", [])])}
      {triage_html}
    </div>
  </details>
</article>"""


def _diagnostic_row(diagnostic: Mapping[str, Any]) -> str:
    css = "blocking" if diagnostic.get("blocking") else ""
    detail = diagnostic.get("detail") or ""
    tail = f"<br><small>{_esc(detail)}</small>" if detail else ""
    return (
        f'<li class="{css}"><code>{_esc(diagnostic.get("code", ""))}</code> '
        f"{_esc(diagnostic.get('message', ''))}{tail}</li>"
    )


def _caveats(payload: Mapping[str, Any]) -> str:
    """What the run could not check, stated on the page rather than in a footnote."""
    parts: list[str] = []
    degraded = payload.get("degraded")
    if isinstance(degraded, dict):
        skipped = degraded.get("skipped") or []
        rows = "".join(
            f"<li><code>{_esc(item.get('rule_id', ''))}</code> {_esc(item.get('title', ''))}"
            f"<br><small>{_esc(item.get('fallback', ''))}</small></li>"
            for item in skipped
        )
        parts.append(
            f'<div class="caveat warn"><h3>Not fully checked</h3>'
            f"<p>{_esc(degraded.get('reason', ''))}</p>"
            f"{f'<ul>{rows}</ul>' if rows else ''}</div>"
        )

    diagnostics = [d for d in payload.get("diagnostics", []) if isinstance(d, dict)]
    if diagnostics:
        rows = "".join(_diagnostic_row(d) for d in diagnostics)
        parts.append(f'<div class="caveat"><h3>Diagnostics</h3><ul>{rows}</ul></div>')

    rule_errors = payload.get("rule_errors") or {}
    if isinstance(rule_errors, dict) and rule_errors:
        rows = "".join(
            f"<li><code>{_esc(k)}</code> {_esc(v)}</li>" for k, v in sorted(rule_errors.items())
        )
        parts.append(f'<div class="caveat warn"><h3>Rules that crashed</h3><ul>{rows}</ul></div>')

    parse_errors = payload.get("parse_errors") or {}
    if isinstance(parse_errors, dict) and parse_errors:
        rows = "".join(
            f"<li><code>{_esc(k)}</code> {_esc(v)}</li>" for k, v in sorted(parse_errors.items())
        )
        parts.append(
            f'<div class="caveat warn"><h3>Files that could not be parsed</h3><ul>{rows}</ul></div>'
        )

    return "".join(parts)


def _filters(payload: Mapping[str, Any]) -> str:
    findings = [f for f in payload.get("findings", []) if isinstance(f, dict)]
    present_sev = {str(f.get("severity", "")) for f in findings}
    present_fam = {str(f.get("family", "")) for f in findings}

    sev_boxes = "".join(
        f'<label class="chk"><input type="checkbox" data-filter="severity" value="{_esc(s)}" '
        f"checked> {_esc(s)}</label>"
        for s in _SEVERITIES
        if s in present_sev
    )
    conf_boxes = "".join(
        f'<label class="chk"><input type="checkbox" data-filter="confidence" value="{_esc(c)}" '
        f"checked> {_esc(c)}</label>"
        for c in _CONFIDENCES
        if c in {str(f.get("confidence", "")) for f in findings}
    )
    fam_boxes = "".join(
        f'<label class="chk"><input type="checkbox" data-filter="family" value="{_esc(f)}" '
        f"checked> {_esc(f)}</label>"
        for f in sorted(present_fam)
    )
    return f"""<section class="filters">
  <input type="search" id="q" placeholder="Search rule, title, message, file, fingerprint">
  <div class="groups">
    <fieldset><legend>Severity</legend>{sev_boxes}</fieldset>
    <fieldset><legend>Confidence</legend>{conf_boxes}</fieldset>
    <fieldset><legend>Family</legend>{fam_boxes}</fieldset>
  </div>
  <p class="count"><span id="shown">{len(findings)}</span> of {len(findings)} shown</p>
</section>"""


def _summary(payload: Mapping[str, Any]) -> str:
    summary = payload.get("summary", {})
    by_sev = summary.get("by_severity", {}) if isinstance(summary, dict) else {}
    tiles = "".join(
        f'<div class="tile sev-{_esc(s)}"><b>{_esc(by_sev.get(s, 0))}</b>'
        f"<span>{_esc(s)}</span></div>"
        for s in _SEVERITIES
        if by_sev.get(s)
    )
    if not tiles:
        tiles = '<div class="tile none"><b>0</b><span>findings</span></div>'
    return f'<section class="tiles">{tiles}</section>'


_CSS = """
:root{--bg:#0f1115;--fg:#e6e6e6;--dim:#9aa0a6;--card:#171a21;--line:#262b36;
--crit:#ff5c5c;--high:#ff9f43;--med:#ffd93d;--low:#4dabf7;--info:#868e96;--ok:#51cf66}
@media(prefers-color-scheme:light){:root{--bg:#fff;--fg:#1a1a1a;--dim:#5f6368;
--card:#f6f7f9;--line:#e2e5ea}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:1040px;margin:0 auto;padding:32px 20px 96px}
header h1{margin:0 0 4px;font-size:24px}
header .sub{color:var(--dim);margin:0 0 20px;font-size:13px}
header .sub code{color:var(--fg)}
code,pre{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.tiles{display:flex;gap:10px;flex-wrap:wrap;margin:0 0 18px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:10px 16px;min-width:92px}
.tile b{display:block;font-size:22px;line-height:1.2}
.tile span{color:var(--dim);font-size:12px;text-transform:uppercase;letter-spacing:.04em}
.tile.sev-critical b{color:var(--crit)}.tile.sev-high b{color:var(--high)}
.tile.sev-medium b{color:var(--med)}.tile.sev-low b{color:var(--low)}
.tile.sev-info b{color:var(--info)}.tile.none b{color:var(--ok)}
.stats{color:var(--dim);font-size:13px;margin:0 0 18px}
.caveat{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--dim);
border-radius:6px;padding:12px 16px;margin:0 0 12px}
.caveat.warn{border-left-color:var(--high)}
.caveat h3{margin:0 0 6px;font-size:13px;text-transform:uppercase;letter-spacing:.04em;
color:var(--dim)}
.caveat ul{margin:6px 0 0;padding-left:18px}
.caveat li.blocking{color:var(--crit)}
.caveat small{color:var(--dim)}
.filters{background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:14px 16px;margin:18px 0}
#q{width:100%;padding:9px 12px;border-radius:6px;border:1px solid var(--line);
background:var(--bg);color:var(--fg);font-size:14px}
.groups{display:flex;gap:22px;flex-wrap:wrap;margin-top:12px}
fieldset{border:0;padding:0;margin:0}
legend{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.05em;
padding:0 0 5px}
.chk{display:inline-flex;align-items:center;gap:5px;margin:0 10px 4px 0;font-size:13px}
.count{color:var(--dim);font-size:12px;margin:10px 0 0}
.finding{background:var(--card);border:1px solid var(--line);border-radius:8px;
margin:0 0 8px;overflow:hidden}
.finding[hidden]{display:none}
summary{cursor:pointer;padding:11px 14px;display:flex;align-items:center;gap:10px;
list-style:none}
summary::-webkit-details-marker{display:none}
summary:hover{background:rgba(127,127,127,.07)}
.sev{width:8px;height:8px;border-radius:50%;flex:0 0 auto}
.sev-critical{background:var(--crit)}.sev-high{background:var(--high)}
.sev-medium{background:var(--med)}.sev-low{background:var(--low)}
.sev-info{background:var(--info)}
.rid{color:var(--dim);font-size:12px;flex:0 0 auto}
.ftitle{flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.where{color:var(--dim);font-size:12px;flex:0 0 auto}
.body{padding:4px 14px 16px;border-top:1px solid var(--line)}
.msg{margin:12px 0}
pre{background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:10px 12px;
overflow-x:auto;font-size:12.5px;margin:8px 0}
.meta{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin:10px 0}
.badge{font-size:11px;padding:2px 8px;border-radius:99px;border:1px solid var(--line);
color:var(--dim)}
.badge.sev-critical{color:var(--crit);border-color:var(--crit)}
.badge.sev-high{color:var(--high);border-color:var(--high)}
.badge.sev-medium{color:var(--med);border-color:var(--med)}
.badge.sev-low{color:var(--low);border-color:var(--low)}
.fp{color:var(--dim);font-size:11px;margin-left:auto}
.block{margin:14px 0 0}
.block h4{margin:0 0 4px;font-size:11px;text-transform:uppercase;letter-spacing:.05em;
color:var(--dim)}
.block p{margin:0}
.ev figcaption{color:var(--dim);font-size:11px}
.refs{margin:0;padding-left:18px}.refs code{font-size:12px;color:var(--dim)}
.kv{display:grid;grid-template-columns:auto 1fr;gap:2px 12px;margin:0;font-size:13px}
.kv dt{color:var(--dim)}.kv dd{margin:0}
.empty{background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:28px;text-align:center;color:var(--dim)}
footer{color:var(--dim);font-size:12px;margin-top:28px;border-top:1px solid var(--line);
padding-top:14px}
"""

# No `innerHTML`, no `eval`, no network. Filtering toggles `hidden` on elements
# that are already in the document, so nothing here can turn an escaped string
# back into markup.
_JS = """
(function(){
  var q=document.getElementById('q');
  var shown=document.getElementById('shown');
  var items=Array.prototype.slice.call(document.querySelectorAll('.finding'));
  var boxes=Array.prototype.slice.call(document.querySelectorAll('input[data-filter]'));
  function allowed(kind){
    var on={};
    boxes.forEach(function(b){ if(b.dataset.filter===kind && b.checked){ on[b.value]=1; } });
    return on;
  }
  function apply(){
    var sev=allowed('severity'), conf=allowed('confidence'), fam=allowed('family');
    var term=(q.value||'').toLowerCase().trim();
    var n=0;
    items.forEach(function(el){
      var ok = sev[el.dataset.severity] && conf[el.dataset.confidence] && fam[el.dataset.family]
        && (term==='' || el.dataset.text.indexOf(term)!==-1);
      el.hidden = !ok;
      if(ok){ n++; }
    });
    shown.textContent = String(n);
  }
  q.addEventListener('input', apply);
  boxes.forEach(function(b){ b.addEventListener('change', apply); });
  apply();
})();
"""


def render(result: RunResult, verdicts: Mapping[str, Verdict] | None = None) -> str:
    payload = json_reporter.build(result, verdicts)
    project = payload.get("project", {})
    summary = payload.get("summary", {})
    findings = [f for f in payload.get("findings", []) if isinstance(f, dict)]
    tool = payload.get("tool", {})

    body = "".join(_finding(f, i) for i, f in enumerate(findings))
    if not body:
        body = (
            '<div class="empty"><p><b>No findings at or above the threshold.</b></p>'
            "<p>That is not the same as no defects. It means 87 rules found nothing "
            "they can see at this severity and confidence. Read the caveats above "
            "before treating it as a clean bill of health.</p></div>"
        )

    below = summary.get("below_threshold", 0)
    stats = (
        f"{_esc(summary.get('reported', 0))} reported · "
        f"{_esc(below)} below threshold · "
        f"{_esc(summary.get('rules_run', 0))} rules run · "
        f"{_esc(summary.get('duration_seconds', 0))}s"
    )

    django_version = project.get("django_version") or "not detected"
    entrypoint = project.get("settings_entrypoint") or "not detected"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>djaudit — {_esc(project.get("root", ""))}</title>
<style>{_CSS}</style>
</head>
<body>
<div class="wrap">
<header>
  <h1>djaudit report</h1>
  <p class="sub"><code>{_esc(project.get("root", ""))}</code> · Django {_esc(django_version)}
   · settings <code>{_esc(entrypoint)}</code> · {_esc(project.get("python_files", 0))} Python files
   · djaudit {_esc(tool.get("version", ""))} · schema {_esc(payload.get("schema_version", ""))}</p>
</header>
{_summary(payload)}
<p class="stats">{stats}</p>
{_caveats(payload)}
{_filters(payload)}
<main>{body}</main>
<footer>
  <p>Generated by djaudit. Every finding here was produced by deterministic
  analysis of your source; no model wrote any of it. Severity is how much damage
  the finding does, confidence is how sure the rule is that it is real, and the
  two are deliberately separate.</p>
  <p>This file contains the full JSON payload it was rendered from, so it is
  also the machine-readable report.</p>
</footer>
</div>
<script type="application/json" id="djaudit-payload">{_embed(payload)}</script>
<script>{_JS}</script>
</body>
</html>
"""

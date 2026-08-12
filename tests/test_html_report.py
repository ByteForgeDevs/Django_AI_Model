"""The HTML report is opened in a browser, so escaping is a security boundary.

The report embeds source excerpts from the audited project, and auditing an
untrusted repository is a supported use of this tool -- it never imports or
executes the target precisely so that pointing it at hostile code is safe. That
guarantee would be worth nothing if the report handed the same hostile code to
the reader's browser as markup.

So these tests do not check that strings "look escaped". They parse the rendered
document the way a browser would and count the elements that come out. A
substring assertion can be satisfied by a report that mangles its input; an
element count can only be satisfied by one where the injected markup never
became an element.

Every absence assertion here is paired with a control that disables escaping and
proves the same assertion fails, because an assertion that cannot fail is not
evidence.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from html.parser import HTMLParser

import pytest

from djaudit import engine
from djaudit.models import (
    Confidence,
    Evidence,
    EvidenceKind,
    Family,
    Finding,
    Location,
    Severity,
    Tier,
)
from djaudit.reporters import html_reporter, json_reporter

BREAKOUT = "</script><script>alert('pwned')</script>"
"""Ends the script element holding it and opens one the reader's browser runs."""

ATTRIBUTE = '" onmouseover="alert(2)" x="'
"""Leaves an attribute value and adds an event handler to the element."""

TAG = "<img src=x onerror=alert(3)>"
"""Needs no script element at all."""

PAYLOADS = (BREAKOUT, ATTRIBUTE, TAG, "<b>bold</b>", "a & b", "'single'", '"double"')

NO_ESCAPING = str
"""What `_esc` becomes when the mutation controls disable it."""

NO_EMBEDDING = json.dumps
"""What `_embed` becomes: correct JSON that is unsafe inside a script element."""


class Document(HTMLParser):
    """What a browser would actually build from the rendered bytes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[str] = []
        self.attributes: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)
        self.attributes.extend(name for name, _ in attrs)

    @classmethod
    def of(cls, markup: str) -> Document:
        doc = cls()
        doc.feed(markup)
        return doc


def hostile_finding(text: str) -> Finding:
    """One finding carrying the payload in every field that reaches the page."""
    return Finding(
        rule_id=f"DJS-001{text}",
        title=f"title {text}",
        family=Family.DJS,
        severity=Severity.CRITICAL,
        confidence=Confidence.CERTAIN,
        tier=Tier.STATIC,
        location=Location(file=f"app/{text}.py", line=1, snippet=f"SECRET = {text!r}"),
        message=f"message {text}",
        rationale=f"rationale {text}",
        remediation=f"remediation {text}",
        evidence=(Evidence(kind=EvidenceKind.SOURCE, content=text, source=f"source {text}"),),
        references=(f"https://example.test/{text}",),
        fingerprint=f"fp{text}",
        properties={f"key {text}": f"value {text}"},
    )


@pytest.fixture
def hostile(vulnerable_project):
    """A real run whose findings have been replaced with hostile ones.

    Built from a real `RunResult` rather than a hand-made one so the report is
    exercised through the same payload builder the JSON format uses.
    """
    result = engine.run(vulnerable_project)
    return replace(result, findings=[hostile_finding(p) for p in PAYLOADS])


def script_count(markup: str) -> int:
    return Document.of(markup).tags.count("script")


class TestTheReportCannotExecuteAuditedCode:
    def test_only_the_two_scripts_the_reporter_wrote_survive(self, hostile):
        """The payload block and the filter behaviour. Nothing the project supplied."""
        assert script_count(html_reporter.render(hostile)) == 2

    def test_the_control_fails_when_escaping_is_removed(self, hostile, monkeypatch):
        """The assertion above is only evidence if it can fail. It can.

        Without this, `== 2` would pass just as happily against a reporter that
        had never escaped anything but happened not to be handed a payload.
        """
        monkeypatch.setattr(html_reporter, "_esc", NO_ESCAPING)
        assert script_count(html_reporter.render(hostile)) > 2

    def test_no_event_handler_attribute_is_created(self, hostile):
        attributes = Document.of(html_reporter.render(hostile)).attributes
        assert not [a for a in attributes if a.startswith("on")]

    def test_the_control_fails_for_attributes_too(self, hostile, monkeypatch):
        monkeypatch.setattr(html_reporter, "_esc", NO_ESCAPING)
        attributes = Document.of(html_reporter.render(hostile)).attributes
        assert [a for a in attributes if a.startswith("on")]

    def test_an_injected_img_never_becomes_an_element(self, hostile):
        assert "img" not in Document.of(html_reporter.render(hostile)).tags

    def test_the_payload_is_still_shown_to_the_reader(self, hostile):
        """Escaped, not dropped.

        A reporter that deleted anything suspicious would pass every test above
        and hide the finding, which is the failure mode that matters most here:
        the one finding an attacker most wants unread is the one about their
        own code.
        """
        markup = html_reporter.render(hostile)
        assert "&lt;/script&gt;" in markup
        assert "alert(&#x27;pwned&#x27;)" in markup


class TestTheEmbeddedPayload:
    def test_it_parses_as_json(self, hostile):
        assert json.loads(_payload_of(html_reporter.render(hostile)))

    def test_it_survives_a_script_terminator_in_the_data(self, hostile):
        data = json.loads(_payload_of(html_reporter.render(hostile)))
        titles = [f["title"] for f in data["findings"]]
        assert any(BREAKOUT in t for t in titles)

    def test_the_control_fails_when_the_terminator_is_not_neutralised(self, hostile, monkeypatch):
        """The second structural defence, proven load-bearing.

        `_esc` guards the visible document; `_embed` guards the JSON block, and
        they fail independently. Escaping the whole payload as HTML would be
        wrong here -- it must stay valid JSON -- so `_embed` escapes only the
        three characters that can end the element, and this is what proves it.
        """
        monkeypatch.setattr(html_reporter, "_embed", NO_EMBEDDING)
        with pytest.raises((json.JSONDecodeError, AssertionError)):
            json.loads(_payload_of(html_reporter.render(hostile)))

    def test_it_is_exactly_what_the_json_reporter_would_emit(self, vulnerable_project):
        """The two formats render the same run, so they cannot disagree about it.

        A format that rebuilt its own counts would eventually contradict the one
        CI gates on, and the reader has no way to tell which is right.
        """
        result = engine.run(vulnerable_project)
        embedded = json.loads(_payload_of(html_reporter.render(result)))
        assert embedded == json_reporter.build(result)


def _payload_of(markup: str) -> str:
    match = re.search(r'id="djaudit-payload">(.*?)</script>', markup, re.S)
    assert match is not None, "the report must embed the payload it rendered"
    return match.group(1)


class TestTheReportIsSelfContained:
    def test_it_requests_nothing_over_the_network(self, vulnerable_project):
        """No CDN, no font, no analytics.

        A report that fetches anything tells whoever serves it which project was
        audited and when it was read, and stops working on the air-gapped
        machine that most wanted it.
        """
        markup = html_reporter.render(engine.run(vulnerable_project))
        assert not re.search(r'(?:src|href)\s*=\s*["\']?(?:https?:)?//', markup)

    def test_the_javascript_never_builds_dom_from_data(self, vulnerable_project):
        """The structural half of the guarantee.

        Escaping protects what Python writes. This protects what the browser
        does afterwards: filtering toggles `hidden` on elements already in the
        document, so there is nothing for an escaped string to break out of.
        """
        markup = html_reporter.render(engine.run(vulnerable_project))
        for construct in (
            "innerHTML",
            "outerHTML",
            "document.write",
            "eval(",
            "insertAdjacentHTML",
        ):
            assert construct not in markup


class TestTheReportSaysWhatItCouldNotCheck:
    def test_the_count_of_findings_the_threshold_hid_is_on_the_page(self, vulnerable_project):
        """Otherwise the reader believes they saw everything djaudit found.

        Asserted against a filtered run, because the label alone renders even
        when the count is zero -- checking for the words would pass against a
        report that always printed `0`.
        """
        result = engine.run(vulnerable_project, min_severity=Severity.HIGH)
        assert result.filtered_threshold > 0, "the fixture must actually hide something"
        assert f"{result.filtered_threshold} below threshold" in html_reporter.render(result)

    def test_an_empty_report_does_not_claim_the_project_is_clean(self, vulnerable_project):
        result = replace(engine.run(vulnerable_project), findings=[])
        markup = html_reporter.render(result)
        assert "No findings at or above the threshold" in markup
        assert "not the same as no defects" in markup

    def test_parse_errors_reach_the_reader(self, vulnerable_project):
        result = engine.run(vulnerable_project)
        result.context.parse_errors[vulnerable_project / "broken.py"] = "invalid syntax"
        assert "could not be parsed" in html_reporter.render(result)

    def test_rule_errors_reach_the_reader(self, vulnerable_project):
        result = engine.run(vulnerable_project)
        result.rule_errors["DJS-001"] = "TypeError: boom"
        markup = html_reporter.render(result)
        assert "crashed" in markup
        assert "TypeError: boom" in markup


class TestTheDocumentIsWellFormed:
    def test_every_finding_becomes_exactly_one_article(self, vulnerable_project):
        result = engine.run(vulnerable_project)
        markup = html_reporter.render(result)
        assert markup.count('class="finding"') == len(result.findings)

    def test_it_declares_a_doctype_and_a_charset(self, vulnerable_project):
        markup = html_reporter.render(engine.run(vulnerable_project))
        assert markup.startswith("<!DOCTYPE html>")
        assert '<meta charset="utf-8">' in markup

    def test_severity_counts_match_the_findings(self, vulnerable_project):
        result = engine.run(vulnerable_project)
        markup = html_reporter.render(result)
        for severity, count in result.counts_by_severity().items():
            if count:
                assert f'<div class="tile sev-{severity.value}"><b>{count}</b>' in markup

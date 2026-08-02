"""The finding schema is the project's central contract; these lock its shape."""

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


def make_finding(**overrides) -> Finding:
    defaults = {
        "rule_id": "DJS-001",
        "title": "t",
        "family": Family.DJS,
        "severity": Severity.HIGH,
        "confidence": Confidence.FIRM,
        "tier": Tier.STATIC,
        "location": Location(file="a.py", line=1),
        "message": "m",
    }
    return Finding(**{**defaults, **overrides})


class TestSeverity:
    def test_ranks_are_ordered(self):
        ranks = [s.rank for s in Severity]
        assert ranks == sorted(ranks, reverse=True)

    def test_security_severity_decreases_with_severity(self):
        scores = [s.security_severity for s in Severity]
        assert scores == sorted(scores, reverse=True)


class TestConfidence:
    def test_certain_outranks_firm_outranks_tentative(self):
        assert Confidence.CERTAIN.rank > Confidence.FIRM.rank > Confidence.TENTATIVE.rank


class TestFamily:
    def test_every_family_has_a_label(self):
        assert all(f.label for f in Family)


class TestFindingOrdering:
    def test_severity_dominates(self):
        critical = make_finding(severity=Severity.CRITICAL, confidence=Confidence.TENTATIVE)
        low = make_finding(severity=Severity.LOW, confidence=Confidence.CERTAIN)
        assert sorted([low, critical], key=lambda f: f.sort_key)[0] is critical

    def test_confidence_breaks_severity_ties(self):
        certain = make_finding(confidence=Confidence.CERTAIN)
        tentative = make_finding(confidence=Confidence.TENTATIVE)
        assert sorted([tentative, certain], key=lambda f: f.sort_key)[0] is certain

    def test_position_breaks_remaining_ties(self):
        first = make_finding(location=Location(file="a.py", line=1))
        second = make_finding(location=Location(file="a.py", line=9))
        assert sorted([second, first], key=lambda f: f.sort_key)[0] is first


class TestSerialisation:
    def test_to_dict_round_trips_enum_values_as_strings(self):
        payload = make_finding(
            evidence=(Evidence(kind=EvidenceKind.SQL, content="SELECT 1", source="psql"),),
            references=("https://example.test",),
        ).to_dict()

        assert payload["severity"] == "high"
        assert payload["confidence"] == "firm"
        assert payload["family"] == "DJS"
        assert payload["tier"] == "static"
        assert payload["evidence"] == [
            {"kind": "sql", "content": "SELECT 1", "source": "psql"}
        ]
        assert payload["references"] == ["https://example.test"]

    def test_with_fingerprint_does_not_mutate_the_original(self):
        original = make_finding()
        stamped = original.with_fingerprint("abc123")
        assert original.fingerprint == ""
        assert stamped.fingerprint == "abc123"

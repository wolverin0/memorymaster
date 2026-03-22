"""Tests for QMD ↔ memorymaster claim mapping."""

from __future__ import annotations


from memorymaster.qmd_bridge import claims_to_qmd, qmd_to_claims


class TestQmdToClaims:
    """Test QMD to claims conversion."""

    def test_qmd_to_claims_empty_list(self):
        """Empty QMD list returns empty claims list."""
        result = qmd_to_claims([])
        assert result == []

    def test_qmd_to_claims_single_fact(self):
        """Single QMD fact is converted."""
        qmd = [{"text": "Python is a language", "type": "fact", "tier": "core"}]
        result = qmd_to_claims(qmd)
        assert len(result) == 1
        assert result[0]["text"] == "Python is a language"
        assert result[0]["claim_type"] == "fact"
        assert result[0]["scope"] == "global"
        assert result[0]["confidence"] == 0.7

    def test_qmd_to_claims_working_tier(self):
        """Working tier maps to project scope."""
        qmd = [{"text": "Note", "type": "fact", "tier": "working"}]
        result = qmd_to_claims(qmd)
        assert result[0]["scope"] == "project"
        assert result[0]["confidence"] == 0.5

    def test_qmd_to_claims_peripheral_tier(self):
        """Peripheral tier maps to project scope."""
        qmd = [{"text": "Note", "type": "fact", "tier": "peripheral"}]
        result = qmd_to_claims(qmd)
        assert result[0]["scope"] == "project"

    def test_qmd_to_claims_all_types(self):
        """All QMD types are mapped correctly."""
        qmd_types = ["fact", "event", "procedure", "constraint", "commitment", "preference"]
        for qmd_type in qmd_types:
            qmd = [{"text": "Test", "type": qmd_type, "tier": "core"}]
            result = qmd_to_claims(qmd)
            assert result[0]["claim_type"] == qmd_type

    def test_qmd_to_claims_unknown_type_defaults_to_fact(self):
        """Unknown type defaults to fact."""
        qmd = [{"text": "Test", "type": "unknown", "tier": "core"}]
        result = qmd_to_claims(qmd)
        assert result[0]["claim_type"] == "fact"

    def test_qmd_to_claims_skips_empty_text(self):
        """Empty text entries are skipped."""
        qmd = [
            {"text": "", "type": "fact", "tier": "core"},
            {"text": "   ", "type": "fact", "tier": "core"},
            {"text": "Valid", "type": "fact", "tier": "core"},
        ]
        result = qmd_to_claims(qmd)
        assert len(result) == 1
        assert result[0]["text"] == "Valid"

    def test_qmd_to_claims_includes_citation(self):
        """Each claim includes a citation source."""
        qmd = [{"text": "Test", "type": "fact", "tier": "core"}]
        result = qmd_to_claims(qmd, source="custom-source")
        assert "citations" in result[0]
        assert len(result[0]["citations"]) == 1
        assert result[0]["citations"][0].source == "custom-source"

    def test_qmd_to_claims_idempotency_key(self):
        """Each claim has an idempotency key."""
        qmd = [{"text": "Unique text 123", "type": "fact", "tier": "core"}]
        result = qmd_to_claims(qmd)
        assert "idempotency_key" in result[0]
        # Same text should produce same idempotency key
        result2 = qmd_to_claims(qmd)
        assert result[0]["idempotency_key"] == result2[0]["idempotency_key"]

    def test_qmd_to_claims_different_text_different_key(self):
        """Different text produces different idempotency key."""
        qmd1 = [{"text": "Text A", "type": "fact", "tier": "core"}]
        qmd2 = [{"text": "Text B", "type": "fact", "tier": "core"}]
        result1 = qmd_to_claims(qmd1)
        result2 = qmd_to_claims(qmd2)
        assert result1[0]["idempotency_key"] != result2[0]["idempotency_key"]

    def test_qmd_to_claims_multiple_entries(self):
        """Multiple QMD entries are all converted."""
        qmd = [
            {"text": "Fact 1", "type": "fact", "tier": "core"},
            {"text": "Event 2", "type": "event", "tier": "working"},
            {"text": "Procedure 3", "type": "procedure", "tier": "peripheral"},
        ]
        result = qmd_to_claims(qmd)
        assert len(result) == 3


class TestClaimsToQmd:
    """Test claims to QMD conversion."""

    def test_claims_to_qmd_empty_list(self):
        """Empty claims list returns empty QMD list."""
        result = claims_to_qmd([])
        assert result == []

    def test_claims_to_qmd_single_claim(self):
        """Single claim is converted to QMD."""
        claim = MockClaim(claim_type="fact", scope="global:system")
        result = claims_to_qmd([claim])
        assert len(result) == 1
        assert result[0]["type"] == "fact"
        assert result[0]["tier"] == "core"

    def test_claims_to_qmd_all_types(self):
        """All claim types are mapped to QMD."""
        claim_types = ["fact", "event", "procedure", "constraint", "commitment", "preference"]
        for ctype in claim_types:
            claim = MockClaim(claim_type=ctype, scope="global")
            result = claims_to_qmd([claim])
            assert result[0]["type"] == ctype

    def test_claims_to_qmd_global_scope_maps_to_core(self):
        """Global scope maps to core tier."""
        claim = MockClaim(claim_type="fact", scope="global")
        result = claims_to_qmd([claim])
        assert result[0]["tier"] == "core"

    def test_claims_to_qmd_project_scope_maps_to_working(self):
        """Project scope maps to working tier."""
        claim = MockClaim(claim_type="fact", scope="project:test")
        result = claims_to_qmd([claim])
        assert result[0]["tier"] == "working"

    def test_claims_to_qmd_unknown_scope_defaults_to_working(self):
        """Unknown scope defaults to working tier."""
        claim = MockClaim(claim_type="fact", scope="unknown:scope")
        result = claims_to_qmd([claim])
        assert result[0]["tier"] == "working"

    def test_claims_to_qmd_multiple_claims(self):
        """Multiple claims are converted."""
        claims = [
            MockClaim(claim_type="fact", scope="global"),
            MockClaim(claim_type="event", scope="project:test"),
        ]
        result = claims_to_qmd(claims)
        assert len(result) == 2

    def test_claims_to_qmd_preserves_text(self):
        """Claim text is preserved in QMD."""
        claim = MockClaim(claim_type="fact", scope="global", text="Important fact")
        result = claims_to_qmd([claim])
        assert result[0]["text"] == "Important fact"


class TestRoundTrip:
    """Test roundtrip conversion."""

    def test_qmd_to_claims_to_qmd(self):
        """QMD → Claims → QMD preserves type and tier."""
        original_qmd = [{"text": "Test claim", "type": "fact", "tier": "core"}]
        claims = qmd_to_claims(original_qmd)
        # Convert claims back to mock objects for QMD conversion
        mock_claims = [
            MockClaim(
                claim_type=c["claim_type"],
                scope=c["scope"],
                text=c["text"],
            )
            for c in claims
        ]
        result_qmd = claims_to_qmd(mock_claims)

        assert result_qmd[0]["type"] == original_qmd[0]["type"]
        assert result_qmd[0]["tier"] == original_qmd[0]["tier"]
        assert result_qmd[0]["text"] == original_qmd[0]["text"]


class MockClaim:
    """Mock Claim for testing."""

    def __init__(self, claim_type=None, scope="global", text="Test"):
        self.claim_type = claim_type
        self.scope = scope
        self.text = text

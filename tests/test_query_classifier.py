"""Tests for query classification."""

from __future__ import annotations

import pytest

from memorymaster.query_classifier import QUERY_TYPES, classify_query, recommended_retrieval_mode


class TestQueryTypes:
    """Test QUERY_TYPES constant."""

    def test_query_types_valid(self):
        """QUERY_TYPES contains all expected types."""
        expected = {
            "fact_lookup",
            "relational",
            "temporal",
            "constraint_check",
            "preference",
            "verification",
            "open_ended",
        }
        assert set(QUERY_TYPES) == expected


class TestClassifyQueryFacts:
    """Test fact_lookup classification."""

    def test_classify_what_question(self):
        """'What' questions are fact_lookup."""
        assert classify_query("What database does pedrito use?") == "fact_lookup"
        assert classify_query("what is your name") == "fact_lookup"

    def test_classify_where_question(self):
        """'Where' questions are fact_lookup."""
        assert classify_query("where is the config file?") == "fact_lookup"
        assert classify_query("WHERE do we store secrets?") == "fact_lookup"

    def test_classify_who_question(self):
        """'Who' questions are fact_lookup."""
        assert classify_query("Who owns this project?") == "fact_lookup"
        assert classify_query("who is responsible?") == "fact_lookup"

    def test_classify_which_question(self):
        """'Which' questions are fact_lookup."""
        assert classify_query("Which framework do we use?") == "fact_lookup"
        assert classify_query("which version?") == "fact_lookup"

    def test_classify_how_many_question(self):
        """'How many' questions are fact_lookup."""
        assert classify_query("How many users are active?") == "fact_lookup"
        assert classify_query("how much time?") == "fact_lookup"


class TestClassifyQueryVerification:
    """Test verification classification."""

    def test_classify_is_it_question(self):
        """'Is it' questions are verification."""
        assert classify_query("Is it safe to deploy?") == "verification"
        assert classify_query("is it true?") == "verification"

    def test_classify_does_it_question(self):
        """'Does it' questions are verification."""
        assert classify_query("Does it support OAuth?") == "verification"
        assert classify_query("does it exist?") == "verification"

    def test_classify_are_there_question(self):
        """'Are there' questions are verification."""
        assert classify_query("Are there any security issues?") == "verification"
        assert classify_query("are there tests?") == "verification"

    def test_classify_can_we_question(self):
        """'Can we' questions are verification."""
        assert classify_query("Can we merge this PR?") == "verification"

    def test_classify_should_we_question(self):
        """'Should we' questions are verification."""
        assert classify_query("Should we refactor this?") == "verification"


class TestClassifyQueryTemporal:
    """Test temporal classification."""

    def test_classify_when_keyword(self):
        """'When' keyword triggers temporal."""
        assert classify_query("When did we deploy v2?") == "temporal"
        assert classify_query("when was this changed?") == "temporal"

    def test_classify_last_week(self):
        """'Last week' triggers temporal."""
        assert classify_query("What changed last week?") == "temporal"
        assert classify_query("last week's updates") == "temporal"

    def test_classify_history_keyword(self):
        """'History' keyword triggers temporal."""
        assert classify_query("Show me the history") == "temporal"
        assert classify_query("give me history") == "temporal"

    def test_classify_yesterday(self):
        """'Yesterday' triggers temporal."""
        assert classify_query("What happened yesterday?") == "temporal"

    def test_classify_timeline(self):
        """'Timeline' triggers temporal."""
        assert classify_query("Create a timeline") == "temporal"


class TestClassifyQueryConstraint:
    """Test constraint_check classification."""

    def test_classify_rule_keyword(self):
        """'Rule' keyword triggers constraint_check."""
        assert classify_query("What are the rules?") == "constraint_check"
        assert classify_query("rule for validation") == "constraint_check"

    def test_classify_must_keyword(self):
        """'Must' keyword triggers constraint_check."""
        assert classify_query("We must validate input") == "constraint_check"
        assert classify_query("must follow protocol") == "constraint_check"

    def test_classify_never_keyword(self):
        """'Never' keyword triggers constraint_check."""
        assert classify_query("Never commit secrets") == "constraint_check"
        assert classify_query("what should never happen") == "constraint_check"

    def test_classify_always_keyword(self):
        """'Always' keyword triggers constraint_check."""
        assert classify_query("Always use HTTPS") == "constraint_check"

    def test_classify_policy_keyword(self):
        """'Policy' keyword triggers constraint_check."""
        assert classify_query("What is the policy?") == "constraint_check"
        assert classify_query("company policy") == "constraint_check"


class TestClassifyQueryPreference:
    """Test preference classification."""

    def test_classify_prefer_keyword(self):
        """'Prefer' keyword triggers preference."""
        assert classify_query("Do you prefer SQL?") == "preference"
        assert classify_query("preferred method") == "preference"

    def test_classify_like_keyword(self):
        """'Like' keyword triggers preference."""
        assert classify_query("How do you like Python?") == "preference"

    def test_classify_convention_keyword(self):
        """'Convention' keyword triggers preference."""
        assert classify_query("What naming convention?") == "preference"
        assert classify_query("coding convention") == "preference"


class TestClassifyQueryRelational:
    """Test relational classification."""

    def test_classify_depends_on(self):
        """'Depends on' triggers relational."""
        assert classify_query("What depends on PaymentService?") == "relational"
        assert classify_query("depends on database") == "relational"

    def test_classify_calls(self):
        """'Calls' keyword triggers relational."""
        assert classify_query("Which service calls this API?") == "relational"

    def test_classify_uses(self):
        """'Uses' keyword triggers relational."""
        assert classify_query("What uses Redis?") == "relational"

    def test_classify_imports(self):
        """'Imports' keyword triggers relational."""
        assert classify_query("What imports this module?") == "relational"

    def test_classify_connected(self):
        """'Connected' keyword triggers relational."""
        assert classify_query("What is connected to this?") == "relational"


class TestClassifyQueryOpenEnded:
    """Test open_ended classification (default)."""

    def test_classify_generic_question(self):
        """Generic questions are open_ended."""
        assert classify_query("Tell me about the architecture") == "open_ended"
        assert classify_query("Summarize the project") == "open_ended"

    def test_classify_empty_string(self):
        """Empty string defaults to open_ended."""
        assert classify_query("") == "open_ended"
        assert classify_query("   ") == "open_ended"

    def test_classify_ambiguous_query(self):
        """Ambiguous query defaults to open_ended."""
        assert classify_query("hello") == "open_ended"
        assert classify_query("anything") == "open_ended"


class TestClassifyQueryCaseSensitivity:
    """Test case-insensitive classification."""

    def test_classify_uppercase(self):
        """Classification ignores case."""
        assert classify_query("WHAT IS THE DATABASE?") == "fact_lookup"
        assert classify_query("WHEN DID THIS HAPPEN?") == "temporal"

    def test_classify_mixed_case(self):
        """Mixed case is handled."""
        assert classify_query("WhAt DaTaBaSe?") == "fact_lookup"


class TestClassifyQueryPriority:
    """Test classification priority when multiple patterns match."""

    def test_temporal_over_fact(self):
        """Temporal patterns take priority over fact patterns."""
        # Both start with "what" and contain "when"
        assert classify_query("What happened when we deployed?") == "temporal"

    def test_verification_over_fact(self):
        """Verification patterns take priority."""
        # Both could be fact, but "Is it" is verification
        assert classify_query("Is it a boolean?") == "verification"


class TestRecommendedRetrievalMode:
    """Test retrieval mode recommendations."""

    def test_fact_lookup_uses_legacy(self):
        """Fact lookup recommends legacy mode."""
        assert recommended_retrieval_mode("fact_lookup") == "legacy"

    def test_relational_uses_qdrant(self):
        """Relational queries recommend qdrant."""
        assert recommended_retrieval_mode("relational") == "qdrant"

    def test_temporal_uses_legacy(self):
        """Temporal queries recommend legacy mode."""
        assert recommended_retrieval_mode("temporal") == "legacy"

    def test_constraint_uses_legacy(self):
        """Constraint queries recommend legacy mode."""
        assert recommended_retrieval_mode("constraint_check") == "legacy"

    def test_preference_uses_qdrant(self):
        """Preference queries recommend qdrant."""
        assert recommended_retrieval_mode("preference") == "qdrant"

    def test_verification_uses_legacy(self):
        """Verification queries recommend legacy mode."""
        assert recommended_retrieval_mode("verification") == "legacy"

    def test_open_ended_uses_qdrant(self):
        """Open-ended queries recommend qdrant."""
        assert recommended_retrieval_mode("open_ended") == "qdrant"

    def test_unknown_type_defaults_legacy(self):
        """Unknown type defaults to legacy."""
        assert recommended_retrieval_mode("unknown_type") == "legacy"


class TestIntegration:
    """Integration tests for classify + recommend."""

    def test_full_pipeline_fact_lookup(self):
        """Classify and recommend for fact lookup."""
        query_type = classify_query("What database does pedrito use?")
        mode = recommended_retrieval_mode(query_type)
        assert query_type == "fact_lookup"
        assert mode == "legacy"

    def test_full_pipeline_semantic(self):
        """Classify and recommend for semantic query."""
        query_type = classify_query("Tell me about the architecture")
        mode = recommended_retrieval_mode(query_type)
        assert query_type == "open_ended"
        assert mode == "qdrant"

    def test_full_pipeline_relational(self):
        """Classify and recommend for relational query."""
        query_type = classify_query("What depends on PaymentService?")
        mode = recommended_retrieval_mode(query_type)
        assert query_type == "relational"
        assert mode == "qdrant"

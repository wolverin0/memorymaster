"""Regression tests for the misc-correctness audit cluster.

Covers three low-severity correctness findings:

1. config.INITIAL_CONFIDENCE_BY_TYPE must not contain markup-laden / non-canonical
   claim_type keys. service.ingest normalizes claim_type via .strip().lower(), so a
   key containing ``</claim_type>`` or ``<parameter ...>`` markup can NEVER match the
   normalized lookup — it is dead calibration data. WHY it matters: dead keys give a
   false impression the type is calibrated and bloat the config surface.

2. query_classifier.classify_query must route constraint questions to
   ``constraint_check`` even when phrased with a verification opener like
   "Are there any rules...". WHY it matters: the verification opener heuristic used to
   fire first, so constraint questions were misclassified as 'verification' and the
   per-type constraint_check retrieval profile was never applied.

3. webhook.fire_webhook must reject non-http(s) URL schemes BEFORE dispatch. WHY it
   matters: urlopen follows whatever scheme the configured URL has, so a file://,
   ftp://, or gopher:// URL is an SSRF / local-file-read primitive.
"""

from __future__ import annotations

import os
from unittest.mock import patch

from memorymaster.config import INITIAL_CONFIDENCE_BY_TYPE
from memorymaster.recall.query_classifier import classify_query
from memorymaster.webhook import fire_webhook


class TestConfidenceCalibrationKeysAreCanonical:
    """Finding 1: no markup-laden / non-canonical claim_type keys."""

    def test_no_markup_in_keys(self):
        """Keys with XML/tool-call markup can never match normalized claim_type."""
        for key in INITIAL_CONFIDENCE_BY_TYPE:
            assert "<" not in key and ">" not in key, f"markup key leaked: {key!r}"
            assert "\n" not in key, f"newline key leaked: {key!r}"

    def test_keys_survive_normalization(self):
        """Every key equals its own .strip().lower() — the lookup form ingest uses."""
        for key in INITIAL_CONFIDENCE_BY_TYPE:
            assert key == key.strip().lower(), f"non-canonical key: {key!r}"

    def test_canonical_types_preserved(self):
        """Removing dead keys must not drop real calibrated types."""
        for canonical in ("constraint", "decision", "gotcha", "finding", "reference"):
            assert canonical in INITIAL_CONFIDENCE_BY_TYPE


class TestConstraintBeatsVerificationOpener:
    """Finding 2: constraint_check evaluated before verification opener."""

    def test_are_there_rules_is_constraint_check(self):
        """'Are there any rules about X?' must classify as constraint_check."""
        assert classify_query("Are there any rules about commits?") == "constraint_check"

    def test_is_there_a_policy_is_constraint_check(self):
        """A verification-opener question containing 'policy' is constraint_check."""
        assert classify_query("Is there a policy we must follow?") == "constraint_check"

    def test_plain_verification_still_verification(self):
        """A verification opener without constraint keywords stays verification."""
        assert classify_query("Is it true that the DB uses WAL?") == "verification"


class TestWebhookSchemeGuard:
    """Finding 3: reject non-http(s) schemes before dispatch."""

    @patch("memorymaster.webhook.urllib.request.urlopen")
    def test_file_scheme_rejected_without_dispatch(self, mock_urlopen):
        """file:// URLs are refused and urlopen is never called."""
        with patch.dict(os.environ, {"MEMORYMASTER_WEBHOOK_URL": "file:///etc/passwd"}):
            assert fire_webhook("evt", {"a": 1}) is False
        mock_urlopen.assert_not_called()

    @patch("memorymaster.webhook.urllib.request.urlopen")
    def test_ftp_scheme_rejected_without_dispatch(self, mock_urlopen):
        """ftp:// URLs are refused and urlopen is never called."""
        with patch.dict(os.environ, {"MEMORYMASTER_WEBHOOK_URL": "ftp://host/x"}):
            assert fire_webhook("evt", {}) is False
        mock_urlopen.assert_not_called()

    @patch("memorymaster.webhook.urllib.request.urlopen")
    def test_https_scheme_still_dispatches(self, mock_urlopen):
        """A valid https URL still passes the guard and reaches urlopen."""
        resp = mock_urlopen.return_value.__enter__.return_value
        resp.status = 200
        with patch.dict(os.environ, {"MEMORYMASTER_WEBHOOK_URL": "https://example.com/h"}):
            assert fire_webhook("evt", {}) is True
        mock_urlopen.assert_called_once()

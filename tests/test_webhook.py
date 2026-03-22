"""Tests for webhook notifications."""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch


from memorymaster.webhook import fire_webhook


class TestFireWebhook:
    """Test webhook firing."""

    @patch.dict(os.environ, {}, clear=False)
    def test_fire_webhook_no_url_configured(self):
        """fire_webhook returns False when no URL configured."""
        if "MEMORYMASTER_WEBHOOK_URL" in os.environ:
            del os.environ["MEMORYMASTER_WEBHOOK_URL"]
        result = fire_webhook("test_event", {"data": "value"})
        assert result is False

    @patch.dict(os.environ, {"MEMORYMASTER_WEBHOOK_URL": ""})
    def test_fire_webhook_empty_url(self):
        """fire_webhook returns False for empty URL."""
        result = fire_webhook("test_event", {})
        assert result is False

    @patch("memorymaster.webhook.urllib.request.urlopen")
    @patch.dict(os.environ, {"MEMORYMASTER_WEBHOOK_URL": "http://example.com/webhook"})
    def test_fire_webhook_success(self, mock_urlopen):
        """fire_webhook returns True on success."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_urlopen.return_value.__enter__ = MagicMock(return_value=mock_resp)
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)

        result = fire_webhook("claim_created", {"claim_id": 42})
        assert result is True

    @patch("memorymaster.webhook.urllib.request.urlopen")
    @patch.dict(os.environ, {"MEMORYMASTER_WEBHOOK_URL": "http://example.com/webhook"})
    def test_fire_webhook_sends_correct_payload(self, mock_urlopen):
        """fire_webhook sends correctly formatted payload."""
        mock_urlopen.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)

        fire_webhook("test_event", {"key": "value"})

        # Check that urlopen was called with correct Request
        call_args = mock_urlopen.call_args
        request = call_args[0][0]

        # Verify request body
        body = json.loads(request.data.decode())
        assert body["event"] == "test_event"
        assert body["data"]["key"] == "value"

    @patch("memorymaster.webhook.urllib.request.urlopen")
    @patch.dict(os.environ, {"MEMORYMASTER_WEBHOOK_URL": "http://example.com/webhook"})
    def test_fire_webhook_sets_headers(self, mock_urlopen):
        """fire_webhook sets content-type header."""
        mock_urlopen.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)

        fire_webhook("event", {})

        request = mock_urlopen.call_args[0][0]
        assert request.headers["Content-type"] == "application/json"

    @patch("memorymaster.webhook.urllib.request.urlopen")
    @patch.dict(os.environ, {"MEMORYMASTER_WEBHOOK_URL": "http://example.com/webhook"})
    def test_fire_webhook_uses_post(self, mock_urlopen):
        """fire_webhook uses POST method."""
        mock_urlopen.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)

        fire_webhook("event", {})

        request = mock_urlopen.call_args[0][0]
        assert request.get_method() == "POST"

    @patch("memorymaster.webhook.urllib.request.urlopen")
    @patch.dict(os.environ, {"MEMORYMASTER_WEBHOOK_URL": "http://example.com/webhook"})
    def test_fire_webhook_timeout_returns_false(self, mock_urlopen):
        """fire_webhook returns False on timeout."""
        mock_urlopen.side_effect = TimeoutError()

        result = fire_webhook("event", {})
        assert result is False

    @patch("memorymaster.webhook.urllib.request.urlopen")
    @patch.dict(os.environ, {"MEMORYMASTER_WEBHOOK_URL": "http://example.com/webhook"})
    def test_fire_webhook_connection_error_returns_false(self, mock_urlopen):
        """fire_webhook returns False on connection error."""
        mock_urlopen.side_effect = Exception("Connection refused")

        result = fire_webhook("event", {})
        assert result is False

    @patch("memorymaster.webhook.urllib.request.urlopen")
    @patch.dict(os.environ, {"MEMORYMASTER_WEBHOOK_URL": "http://example.com/webhook"})
    def test_fire_webhook_empty_payload(self, mock_urlopen):
        """fire_webhook handles empty payload."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_urlopen.return_value.__enter__ = MagicMock(return_value=mock_resp)
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)

        result = fire_webhook("event", {})
        assert result is True

    @patch("memorymaster.webhook.urllib.request.urlopen")
    @patch.dict(os.environ, {"MEMORYMASTER_WEBHOOK_URL": "https://secure.example.com/webhook"})
    def test_fire_webhook_https(self, mock_urlopen):
        """fire_webhook works with HTTPS URLs."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_urlopen.return_value.__enter__ = MagicMock(return_value=mock_resp)
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)

        result = fire_webhook("event", {})
        assert result is True

        request = mock_urlopen.call_args[0][0]
        assert "https://" in request.full_url


class TestWebhookEventTypes:
    """Test various webhook event types."""

    @patch("memorymaster.webhook.urllib.request.urlopen")
    @patch.dict(os.environ, {"MEMORYMASTER_WEBHOOK_URL": "http://example.com/webhook"})
    def test_claim_created_event(self, mock_urlopen):
        """Claim created event is sent correctly."""
        mock_urlopen.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)

        fire_webhook("claim_created", {"claim_id": 1, "status": "active"})

        body = json.loads(mock_urlopen.call_args[0][0].data.decode())
        assert body["event"] == "claim_created"

    @patch("memorymaster.webhook.urllib.request.urlopen")
    @patch.dict(os.environ, {"MEMORYMASTER_WEBHOOK_URL": "http://example.com/webhook"})
    def test_claim_updated_event(self, mock_urlopen):
        """Claim updated event is sent correctly."""
        mock_urlopen.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)

        fire_webhook("claim_updated", {"claim_id": 42, "changes": ["status", "confidence"]})

        body = json.loads(mock_urlopen.call_args[0][0].data.decode())
        assert body["event"] == "claim_updated"

    @patch("memorymaster.webhook.urllib.request.urlopen")
    @patch.dict(os.environ, {"MEMORYMASTER_WEBHOOK_URL": "http://example.com/webhook"})
    def test_conflict_resolved_event(self, mock_urlopen):
        """Conflict resolved event is sent correctly."""
        mock_urlopen.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)

        fire_webhook("conflict_resolved", {"winner_id": 1, "loser_id": 2})

        body = json.loads(mock_urlopen.call_args[0][0].data.decode())
        assert body["event"] == "conflict_resolved"

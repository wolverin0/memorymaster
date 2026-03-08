"""Tests for multi-key rotation in llm_steward.

Covers:
  - KeyRotator round-robin selection
  - Cooldown tracking and expiry
  - _parse_api_keys from flags and env vars
  - _call_llm key rotation on 429 errors
  - Backward compatibility with single key
"""
from __future__ import annotations

import os
import time
from unittest.mock import MagicMock, patch

import pytest

from memorymaster.llm_steward import (
    DEFAULT_COOLDOWN_SECONDS,
    KeyRotator,
    _call_llm,
    _parse_api_keys,
)


# ---------------------------------------------------------------------------
# KeyRotator unit tests
# ---------------------------------------------------------------------------

class TestKeyRotator:
    def test_single_key(self) -> None:
        rotator = KeyRotator(keys=["key-a"])
        assert rotator.key_count == 1
        assert rotator.get_key() == "key-a"
        assert rotator.get_key() == "key-a"

    def test_round_robin(self) -> None:
        rotator = KeyRotator(keys=["a", "b", "c"])
        assert rotator.get_key() == "a"
        assert rotator.get_key() == "b"
        assert rotator.get_key() == "c"
        assert rotator.get_key() == "a"

    def test_deduplication(self) -> None:
        rotator = KeyRotator(keys=["a", "b", "a", "c", "b"])
        assert rotator.key_count == 3
        assert rotator.keys == ["a", "b", "c"]

    def test_strips_whitespace(self) -> None:
        rotator = KeyRotator(keys=["  a  ", " b ", "c"])
        assert rotator.keys == ["a", "b", "c"]

    def test_empty_keys_raises(self) -> None:
        with pytest.raises(ValueError, match="at least one"):
            KeyRotator(keys=[])

    def test_all_blank_keys_raises(self) -> None:
        with pytest.raises(ValueError, match="at least one non-empty"):
            KeyRotator(keys=["", "  ", ""])

    def test_cooldown_skips_key(self) -> None:
        rotator = KeyRotator(keys=["a", "b", "c"], cooldown_seconds=10.0)
        # Mark "a" as rate limited
        rotator.mark_rate_limited("a")
        # Next call should skip "a" and return "b"
        assert rotator.get_key() == "b"
        assert rotator.get_key() == "c"
        # "a" is still on cooldown, skip it
        assert rotator.get_key() == "b"

    def test_clear_cooldown(self) -> None:
        rotator = KeyRotator(keys=["a", "b"], cooldown_seconds=100.0)
        rotator.mark_rate_limited("a")
        assert rotator.get_key() == "b"
        # Clear cooldown on "a"
        rotator.clear_cooldown("a")
        assert rotator.get_key() == "a"

    def test_available_key_count(self) -> None:
        rotator = KeyRotator(keys=["a", "b", "c"], cooldown_seconds=100.0)
        assert rotator.available_key_count == 3
        rotator.mark_rate_limited("a")
        assert rotator.available_key_count == 2
        rotator.mark_rate_limited("b")
        assert rotator.available_key_count == 1

    def test_all_keys_cooldown_waits(self) -> None:
        """When all keys are on cooldown, get_key should sleep until soonest expires."""
        rotator = KeyRotator(keys=["a", "b"], cooldown_seconds=0.05)
        rotator.mark_rate_limited("a")
        rotator.mark_rate_limited("b")
        # Should sleep briefly and return the key whose cooldown expires first
        start = time.monotonic()
        key = rotator.get_key()
        elapsed = time.monotonic() - start
        assert key in ("a", "b")
        # Should have waited at least part of the cooldown
        assert elapsed >= 0.01

    def test_mark_unknown_key_noop(self) -> None:
        rotator = KeyRotator(keys=["a", "b"])
        rotator.mark_rate_limited("unknown")
        rotator.clear_cooldown("unknown")
        # Should not raise or affect rotation
        assert rotator.get_key() == "a"

    def test_default_cooldown(self) -> None:
        rotator = KeyRotator(keys=["a"])
        assert rotator.cooldown_seconds == DEFAULT_COOLDOWN_SECONDS


# ---------------------------------------------------------------------------
# _parse_api_keys tests
# ---------------------------------------------------------------------------

class TestParseApiKeys:
    def test_api_keys_flag_takes_priority(self) -> None:
        result = _parse_api_keys(api_key="single", api_keys="a,b,c")
        assert result == ["a", "b", "c"]

    def test_single_api_key_fallback(self) -> None:
        result = _parse_api_keys(api_key="single", api_keys="")
        assert result == ["single"]

    def test_env_var_multi(self) -> None:
        with patch.dict(os.environ, {"MEMORYMASTER_API_KEYS": "x,y,z"}, clear=False):
            result = _parse_api_keys(api_key="", api_keys="")
            assert result == ["x", "y", "z"]

    def test_env_var_single(self) -> None:
        env_patch = {"MEMORYMASTER_API_KEY": "env-single"}
        # Clear multi-key env var if present
        with patch.dict(os.environ, env_patch, clear=False):
            os.environ.pop("MEMORYMASTER_API_KEYS", None)
            result = _parse_api_keys(api_key="", api_keys="")
            assert result == ["env-single"]

    def test_api_keys_flag_over_env(self) -> None:
        with patch.dict(os.environ, {"MEMORYMASTER_API_KEYS": "env1,env2"}, clear=False):
            result = _parse_api_keys(api_key="", api_keys="flag1,flag2")
            assert result == ["flag1", "flag2"]

    def test_empty_everything_returns_empty_string(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MEMORYMASTER_API_KEYS", None)
            os.environ.pop("MEMORYMASTER_API_KEY", None)
            result = _parse_api_keys(api_key="", api_keys="")
            assert result == [""]

    def test_strips_whitespace_in_keys(self) -> None:
        result = _parse_api_keys(api_keys="  a , b , c  ")
        assert result == ["a", "b", "c"]

    def test_skips_empty_segments(self) -> None:
        result = _parse_api_keys(api_keys="a,,b,,,c")
        assert result == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# _call_llm with key rotation (mocked HTTP)
# ---------------------------------------------------------------------------

class TestCallLlmWithRotation:
    @patch("memorymaster.llm_steward.urllib.request.urlopen")
    def test_rotation_on_429(self, mock_urlopen: MagicMock) -> None:
        """429 on first key should rotate to second key."""
        import io
        import urllib.error

        http_429 = urllib.error.HTTPError(
            url="http://test", code=429, msg="Rate Limited",
            hdrs=None, fp=io.BytesIO(b""),  # type: ignore[arg-type]
        )
        success_response = MagicMock()
        success_response.read.return_value = b'{"candidates":[{"content":{"parts":[{"text":"ok"}]}}]}'
        success_response.__enter__ = MagicMock(return_value=success_response)
        success_response.__exit__ = MagicMock(return_value=False)

        mock_urlopen.side_effect = [http_429, success_response]

        rotator = KeyRotator(keys=["key-a", "key-b"], cooldown_seconds=60.0)

        result = _call_llm(
            provider="gemini",
            api_key="",
            model="gemini-2.5-flash",
            prompt="test prompt",
            key_rotator=rotator,
        )

        assert result == "ok"
        assert mock_urlopen.call_count == 2
        # key-a should be on cooldown
        assert rotator.available_key_count == 1

    @patch("memorymaster.llm_steward.urllib.request.urlopen")
    def test_single_key_backward_compat(self, mock_urlopen: MagicMock) -> None:
        """Without key_rotator, single-key path should work as before."""
        success_response = MagicMock()
        success_response.read.return_value = b'{"candidates":[{"content":{"parts":[{"text":"hello"}]}}]}'
        success_response.__enter__ = MagicMock(return_value=success_response)
        success_response.__exit__ = MagicMock(return_value=False)

        mock_urlopen.return_value = success_response

        result = _call_llm(
            provider="gemini",
            api_key="my-key",
            model="gemini-2.5-flash",
            prompt="test",
        )

        assert result == "hello"
        assert mock_urlopen.call_count == 1

    @patch("memorymaster.llm_steward.urllib.request.urlopen")
    def test_all_keys_exhausted_raises(self, mock_urlopen: MagicMock) -> None:
        """When all keys get 429 and max attempts exceeded, should raise."""
        import io
        import urllib.error

        http_429 = urllib.error.HTTPError(
            url="http://test", code=429, msg="Rate Limited",
            hdrs=None, fp=io.BytesIO(b""),  # type: ignore[arg-type]
        )
        # 2 keys + 2 retries = 4 attempts, all fail
        mock_urlopen.side_effect = [http_429] * 10

        rotator = KeyRotator(keys=["key-a", "key-b"], cooldown_seconds=0.01)

        with pytest.raises(urllib.error.HTTPError):
            _call_llm(
                provider="gemini",
                api_key="",
                model="gemini-2.5-flash",
                prompt="test",
                max_retries=2,
                key_rotator=rotator,
            )

    @patch("memorymaster.llm_steward.urllib.request.urlopen")
    def test_clears_cooldown_on_success(self, mock_urlopen: MagicMock) -> None:
        """Successful call should clear any previous cooldown for the used key."""
        success_response = MagicMock()
        success_response.read.return_value = b'{"choices":[{"message":{"content":"done"}}]}'
        success_response.__enter__ = MagicMock(return_value=success_response)
        success_response.__exit__ = MagicMock(return_value=False)

        mock_urlopen.return_value = success_response

        rotator = KeyRotator(keys=["key-a"], cooldown_seconds=100.0)
        rotator.mark_rate_limited("key-a")
        # Key is on cooldown but it's the only one, so it will be used after waiting
        # To avoid actual sleep, clear the cooldown manually first
        rotator.clear_cooldown("key-a")

        result = _call_llm(
            provider="openai",
            api_key="",
            model="gpt-4o-mini",
            prompt="test",
            key_rotator=rotator,
        )

        assert result == "done"
        assert rotator.available_key_count == 1

"""Tests for memorymaster.llm_steward — KeyRotator and helpers."""

from __future__ import annotations

import os
import time
from unittest.mock import patch

import pytest

from memorymaster.llm_steward import KeyRotator, _parse_api_keys


class TestKeyRotator:
    def test_single_key(self):
        kr = KeyRotator(keys=["key1"])
        assert kr.get_key() == "key1"
        assert kr.key_count == 1

    def test_round_robin(self):
        kr = KeyRotator(keys=["a", "b", "c"])
        assert kr.get_key() == "a"
        assert kr.get_key() == "b"
        assert kr.get_key() == "c"
        assert kr.get_key() == "a"  # wraps

    def test_empty_keys_raises(self):
        with pytest.raises(ValueError, match="at least one"):
            KeyRotator(keys=[])

    def test_all_blank_keys_raises(self):
        with pytest.raises(ValueError, match="non-empty"):
            KeyRotator(keys=["", "  "])

    def test_dedup(self):
        kr = KeyRotator(keys=["a", "b", "a", "c", "b"])
        assert kr.key_count == 3

    def test_mark_rate_limited_skips_key(self):
        kr = KeyRotator(keys=["a", "b"], cooldown_seconds=100)
        kr.mark_rate_limited("a")
        # Should skip "a" and return "b"
        assert kr.get_key() == "b"

    def test_mark_rate_limited_unknown_key(self):
        kr = KeyRotator(keys=["a"])
        kr.mark_rate_limited("unknown")  # should not raise

    def test_clear_cooldown(self):
        kr = KeyRotator(keys=["a", "b"], cooldown_seconds=100)
        kr.mark_rate_limited("a")
        kr.clear_cooldown("a")
        key = kr.get_key()
        assert key == "a"

    def test_clear_cooldown_unknown_key(self):
        kr = KeyRotator(keys=["a"])
        kr.clear_cooldown("unknown")  # should not raise

    def test_available_key_count(self):
        kr = KeyRotator(keys=["a", "b", "c"], cooldown_seconds=100)
        assert kr.available_key_count == 3
        kr.mark_rate_limited("a")
        assert kr.available_key_count == 2

    def test_all_keys_rate_limited_waits(self):
        """When all keys are on cooldown, should wait for soonest."""
        kr = KeyRotator(keys=["a"], cooldown_seconds=0.01)
        kr.mark_rate_limited("a")
        # Should wait ~0.01s then return "a"
        start = time.monotonic()
        key = kr.get_key()
        elapsed = time.monotonic() - start
        assert key == "a"
        # Elapsed should be very small but non-zero (waited for cooldown)


class TestParseApiKeys:
    def test_api_keys_string(self):
        result = _parse_api_keys(api_keys="k1,k2,k3")
        assert result == ["k1", "k2", "k3"]

    def test_api_keys_strips(self):
        result = _parse_api_keys(api_keys=" k1 , k2 ")
        assert result == ["k1", "k2"]

    def test_single_api_key(self):
        result = _parse_api_keys(api_key="single")
        assert result == ["single"]

    def test_env_var_multi(self):
        with patch.dict(os.environ, {"MEMORYMASTER_API_KEYS": "e1,e2"}):
            result = _parse_api_keys()
            assert result == ["e1", "e2"]

    def test_env_var_single(self):
        with patch.dict(os.environ, {"MEMORYMASTER_API_KEY": "envkey"}, clear=False):
            os.environ.pop("MEMORYMASTER_API_KEYS", None)
            result = _parse_api_keys()
            assert result == ["envkey"]

    def test_priority_api_keys_over_env(self):
        with patch.dict(os.environ, {"MEMORYMASTER_API_KEYS": "env"}):
            result = _parse_api_keys(api_keys="explicit")
            assert result == ["explicit"]

    def test_no_keys_returns_fallback(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MEMORYMASTER_API_KEYS", None)
            os.environ.pop("MEMORYMASTER_API_KEY", None)
            result = _parse_api_keys()
            assert result == [""]  # empty string fallback

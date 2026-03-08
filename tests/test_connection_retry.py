"""Tests for connection retry logic with exponential backoff."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from memorymaster.retry import connect_with_retry, _get_retry_config


class TestGetRetryConfig:
    def test_defaults(self):
        with patch.dict("os.environ", {}, clear=True):
            retries, base = _get_retry_config()
        assert retries == 3
        assert base == 0.5

    def test_env_override(self):
        env = {"MEMORYMASTER_DB_RETRIES": "5", "MEMORYMASTER_DB_RETRY_BASE": "1.0"}
        with patch.dict("os.environ", env, clear=True):
            retries, base = _get_retry_config()
        assert retries == 5
        assert base == 1.0

    def test_negative_clamped_to_zero(self):
        env = {"MEMORYMASTER_DB_RETRIES": "-1", "MEMORYMASTER_DB_RETRY_BASE": "-0.5"}
        with patch.dict("os.environ", env, clear=True):
            retries, base = _get_retry_config()
        assert retries == 0
        assert base == 0.0


class TestConnectWithRetry:
    def test_success_on_first_try(self):
        conn = MagicMock()
        result = connect_with_retry(lambda: conn)
        assert result is conn

    @patch("memorymaster.retry.time.sleep")
    def test_success_after_transient_failure(self, mock_sleep):
        conn = MagicMock()
        fn = MagicMock(side_effect=[OSError("locked"), conn])

        env = {"MEMORYMASTER_DB_RETRIES": "3", "MEMORYMASTER_DB_RETRY_BASE": "0.5"}
        with patch.dict("os.environ", env, clear=True):
            result = connect_with_retry(fn)

        assert result is conn
        assert fn.call_count == 2
        mock_sleep.assert_called_once_with(0.5)  # base * 2^0

    @patch("memorymaster.retry.time.sleep")
    def test_all_retries_exhausted(self, mock_sleep):
        error = ConnectionError("refused")
        fn = MagicMock(side_effect=error)

        env = {"MEMORYMASTER_DB_RETRIES": "2", "MEMORYMASTER_DB_RETRY_BASE": "0.25"}
        with patch.dict("os.environ", env, clear=True):
            with pytest.raises(ConnectionError, match="refused"):
                connect_with_retry(fn)

        # 1 initial + 2 retries = 3 total calls
        assert fn.call_count == 3
        # sleep called for attempt 0 and 1 (not after final failure)
        assert mock_sleep.call_count == 2
        mock_sleep.assert_any_call(0.25)   # 0.25 * 2^0
        mock_sleep.assert_any_call(0.5)    # 0.25 * 2^1

    @patch("memorymaster.retry.time.sleep")
    def test_zero_retries_fails_immediately(self, mock_sleep):
        fn = MagicMock(side_effect=RuntimeError("boom"))

        env = {"MEMORYMASTER_DB_RETRIES": "0"}
        with patch.dict("os.environ", env, clear=True):
            with pytest.raises(RuntimeError, match="boom"):
                connect_with_retry(fn)

        assert fn.call_count == 1
        mock_sleep.assert_not_called()

    @patch("memorymaster.retry.time.sleep")
    def test_exponential_backoff_delays(self, mock_sleep):
        conn = MagicMock()
        fn = MagicMock(side_effect=[OSError("1"), OSError("2"), OSError("3"), conn])

        env = {"MEMORYMASTER_DB_RETRIES": "3", "MEMORYMASTER_DB_RETRY_BASE": "0.5"}
        with patch.dict("os.environ", env, clear=True):
            result = connect_with_retry(fn)

        assert result is conn
        assert fn.call_count == 4
        # Delays: 0.5*2^0=0.5, 0.5*2^1=1.0, 0.5*2^2=2.0
        assert mock_sleep.call_args_list[0][0] == (0.5,)
        assert mock_sleep.call_args_list[1][0] == (1.0,)
        assert mock_sleep.call_args_list[2][0] == (2.0,)


class TestSQLiteStoreRetry:
    """Integration: verify SQLiteStore.connect() uses retry wrapper."""

    @patch("memorymaster.retry.time.sleep")
    def test_sqlite_connect_retries_on_failure(self, mock_sleep):
        from memorymaster.storage import SQLiteStore

        store = SQLiteStore(":memory:")

        # Temporarily break sqlite3.connect to simulate failure
        original_connect = __import__("sqlite3").connect
        call_count = 0

        def flaky_connect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise OSError("disk I/O error")
            return original_connect(*args, **kwargs)

        env = {"MEMORYMASTER_DB_RETRIES": "2", "MEMORYMASTER_DB_RETRY_BASE": "0.1"}
        with patch.dict("os.environ", env, clear=True):
            with patch("sqlite3.connect", side_effect=flaky_connect):
                conn = store.connect()

        assert conn is not None
        conn.close()
        assert call_count == 2
        mock_sleep.assert_called_once()

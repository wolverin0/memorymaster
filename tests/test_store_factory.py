"""Tests for memorymaster.store_factory."""

from __future__ import annotations

from pathlib import Path

import pytest

from memorymaster.store_factory import create_store, is_postgres_dsn
from memorymaster.storage import SQLiteStore


class TestIsPostgresDsn:
    def test_postgres_prefix(self):
        assert is_postgres_dsn("postgres://user:pass@host/db") is True

    def test_postgresql_prefix(self):
        assert is_postgres_dsn("postgresql://user:pass@host/db") is True

    def test_case_insensitive(self):
        assert is_postgres_dsn("POSTGRES://HOST/DB") is True

    def test_sqlite_path(self):
        assert is_postgres_dsn("memorymaster.db") is False

    def test_empty(self):
        assert is_postgres_dsn("") is False


class TestCreateStore:
    def test_sqlite_path(self, tmp_path):
        store = create_store(tmp_path / "test.db")
        assert isinstance(store, SQLiteStore)

    def test_sqlite_string(self, tmp_path):
        store = create_store(str(tmp_path / "test.db"))
        assert isinstance(store, SQLiteStore)

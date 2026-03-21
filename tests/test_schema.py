"""Tests for memorymaster.schema — SQL schema loading."""

from memorymaster.schema import load_schema_sql, load_schema_postgres_sql


def test_load_sqlite_schema():
    sql = load_schema_sql()
    assert "CREATE TABLE" in sql
    assert "claims" in sql


def test_load_postgres_schema():
    sql = load_schema_postgres_sql()
    assert "CREATE TABLE" in sql or "CREATE" in sql

from __future__ import annotations

from importlib.resources import files


def load_schema_sql() -> str:
    return files("memorymaster").joinpath("schema.sql").read_text(encoding="utf-8")


def load_schema_postgres_sql() -> str:
    return files("memorymaster").joinpath("schema_postgres.sql").read_text(encoding="utf-8")

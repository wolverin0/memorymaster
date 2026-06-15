"""Backend-parity regression tests for ``wiki-backfill-bindings``.

WHY: the backfill stamps ``claims.wiki_article`` so the wiki READ layer can
link articles back to their source claims. The original handler opened a raw
``sqlite3.connect(effective_db)``. On a Postgres backend that string is a DSN
(``postgresql://...``), so SQLite silently created a junk file named after the
DSN, the SQLite-only SQL no-op'd, and ``claims.wiki_article`` stayed NULL
forever with NO error surfaced to the operator.

These tests anchor the requirement, not the implementation:
  1. on a Postgres DSN the command must fail loudly (never create a junk
     SQLite file, never pretend success);
  2. on SQLite it must route through the backend-aware store connection and
     actually stamp the claims.
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


from memorymaster.surfaces import cli_handlers_curation as C


def _ns(**kw) -> argparse.Namespace:
    base = dict(json_output=False, output="out")
    base.update(kw)
    return argparse.Namespace(**base)


class _FakeService:
    """Minimal service exposing only what the handler touches."""

    def __init__(self, conn):
        self.store = _FakeStore(conn)


class _FakeStore:
    def __init__(self, conn):
        self._conn = conn

    def connect(self):
        return self._conn


def test_postgres_dsn_fails_loudly_and_creates_no_junk_file(tmp_path, capsys):
    """On Postgres the command must error out, not silently create a junk
    SQLite file named after the DSN and report nothing changed."""
    dsn = "postgresql://user:pw@localhost:5432/memorymaster"
    args = _ns(output=str(tmp_path))  # wiki dir exists (tmp_path)

    rc = C._handle_wiki_backfill_bindings(args, _FakeService(None), None, dsn)

    assert rc == 1
    out = capsys.readouterr().out.lower()
    assert "postgres" in out and "not supported" in out
    # The bug signature: a file named after the DSN must NOT appear anywhere.
    assert not any("postgresql" in p.name for p in tmp_path.iterdir())
    assert not Path(dsn).exists()


def test_sqlite_routes_through_store_and_stamps_claims(tmp_path, capsys):
    """On SQLite the handler must use the backend-aware store connection and
    actually backfill claims.wiki_article from article frontmatter."""
    db_path = tmp_path / "claims.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE claims (id INTEGER PRIMARY KEY, text TEXT, "
        "wiki_article TEXT);"
        "INSERT INTO claims (id, text) VALUES (1, 'a'), (2, 'b'), (3, 'c');"
    )
    conn.commit()

    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()
    (wiki_dir / "steward-decay.md").write_text(
        "---\ntitle: Steward decay\nclaims: [1, 2]\n---\nbody\n",
        encoding="utf-8",
    )

    args = _ns(output=str(wiki_dir))
    rc = C._handle_wiki_backfill_bindings(args, _FakeService(conn), None, str(db_path))

    assert rc == 0
    # The handler commits and closes the connection it was handed; re-open
    # to verify the stamp persisted to disk.
    verify = sqlite3.connect(db_path)
    rows = dict(verify.execute("SELECT id, wiki_article FROM claims").fetchall())
    assert rows[1] == "steward-decay"
    assert rows[2] == "steward-decay"
    assert rows[3] is None  # not referenced -> untouched
    verify.close()

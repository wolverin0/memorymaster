"""Recover memorymaster.db (index-only corruption) into a fresh artifact.

The corruption is confined to ONE index (idx_verbatim_session); all base tables
scan cleanly. So a full schema + row copy into a NEW database fully recovers the
data and rebuilds every index from scratch — no row loss.

Strategy:
  1. Open the corrupt DB READ-ONLY (mode=ro), source.
  2. Open a fresh target DB.
  3. Copy the schema in two passes: tables/virtual-tables/triggers first WITHOUT
     indexes, bulk-insert rows, THEN create indexes last (so a corrupt source
     index is never consulted and the target index is built clean from data).
  4. Report per-table row counts.

Read-only on the source; never writes the live DB. Target is a new file.
"""
from __future__ import annotations

import sqlite3
import sys
import time

SRC = "memorymaster.db"
DST = "memorymaster_repaired.db"


def _objects(conn, kind):
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type=? AND sql IS NOT NULL "
        "AND name NOT LIKE 'sqlite_%' ORDER BY rootpage",
        (kind,),
    ).fetchall()
    return [(n, s) for n, s in rows]


def main() -> int:
    t0 = time.time()
    src = sqlite3.connect(f"file:{SRC}?mode=ro", uri=True)
    src.execute("PRAGMA query_only = ON")
    dst = sqlite3.connect(DST)
    dst.execute("PRAGMA journal_mode = OFF")   # bulk load speed; it's a throwaway build
    dst.execute("PRAGMA synchronous = OFF")

    # --- tables (incl. virtual/FTS) first, no indexes yet ---
    tables = _objects(src, "table")
    created = []
    for name, sql in tables:
        try:
            dst.execute(sql)
            created.append(name)
        except sqlite3.Error as exc:
            print(f"  WARN create table {name}: {exc}", file=sys.stderr)

    # --- copy rows table by table; on a malformed bulk read, fall back to a
    #     row-by-row salvage (skip only the individually-unreadable rows). ---
    counts = {}
    lost = {}
    for name in created:
        cols = [r[1] for r in src.execute(f"PRAGMA table_info('{name}')").fetchall()]
        if not cols:
            continue
        placeholders = ",".join("?" * len(cols))
        ins = f"INSERT INTO '{name}' VALUES ({placeholders})"
        try:
            rows = src.execute(f"SELECT * FROM '{name}'").fetchall()
            if rows:
                dst.executemany(ins, rows)
            counts[name] = len(rows)
            continue
        except sqlite3.DatabaseError as exc:
            print(f"  bulk read failed on {name} ({exc}); salvaging row-by-row...", file=sys.stderr)

        # row-by-row salvage by primary-key/rowid
        pk = next((c[1] for c in src.execute(f"PRAGMA table_info('{name}')").fetchall() if c[5]), None)
        keycol = pk or "rowid"
        try:
            maxid = src.execute(f"SELECT max({keycol}) FROM '{name}'").fetchone()[0] or 0
        except sqlite3.DatabaseError:
            maxid = 0
        got = 0
        dropped = 0
        for rid in range(0, maxid + 1):
            try:
                r = src.execute(f"SELECT * FROM '{name}' WHERE {keycol}=?", (rid,)).fetchone()
            except sqlite3.DatabaseError:
                dropped += 1
                continue
            if r is None:
                continue
            try:
                dst.execute(ins, r)
                got += 1
            except sqlite3.Error:
                dropped += 1
        counts[name] = got
        lost[name] = dropped
        dst.commit()
    dst.commit()

    # --- now create indexes + triggers LAST (built clean from the copied data) ---
    for kind in ("index", "trigger"):
        for name, sql in _objects(src, kind):
            try:
                dst.execute(sql)
            except sqlite3.Error as exc:
                print(f"  WARN create {kind} {name}: {exc}", file=sys.stderr)
    dst.commit()

    print(f"recovered tables: {len(created)}; elapsed {time.time()-t0:.0f}s")
    for n in sorted(counts):
        if counts[n]:
            tag = f"  ({lost[n]} rows UNRECOVERABLE)" if lost.get(n) else ""
            print(f"  {n}: {counts[n]} rows{tag}")
    if lost:
        print("LOST (unrecoverable rows):", {k: v for k, v in lost.items() if v})
    src.close()
    dst.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

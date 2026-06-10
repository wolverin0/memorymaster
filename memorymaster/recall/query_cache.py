"""Correctness-safe retrieval result cache (v3.22, gbrain v0.40.3 port).

Opt-in via ``MEMORYMASTER_QUERY_CACHE=1``. A cache entry stores the ordered
result of a hybrid query (claim ids + per-stage scores) keyed by a hash that
folds in the query text, query params, AND a fingerprint of the retrieval
config (weights / mode / floor). It is tagged with the corpus *generation* at
write time (maintained by triggers from migration 0004). On read, the entry is
returned only if the current generation still matches — so any claim write, or
any config change (different key), produces a miss and a fresh compute.

SQLite-only: the read/write helpers use ``?`` placeholders and open a direct
sqlite connection. Postgres deployments simply never cache (the service skips
this layer); the 0004 triggers still maintain the generation there for parity.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

from memorymaster.stores._storage_shared import open_conn
from memorymaster.config import get_config

logger = logging.getLogger(__name__)

_TRUTHY = {"1", "true", "yes", "on"}


def cache_enabled() -> bool:
    return os.environ.get("MEMORYMASTER_QUERY_CACHE", "").strip().lower() in _TRUTHY


def sqlite_db_path(store: Any) -> str | None:
    """Return the store's SQLite file path, or None if it isn't a local SQLite
    store (Postgres DSN / in-memory / unknown) — in which case caching is off."""
    path = getattr(store, "db_path", None)
    if not path:
        return None
    path = str(path)
    if "://" in path or path == ":memory:":
        return None
    return path


def config_fingerprint() -> str:
    """Hash the retrieval-affecting config so a weight/mode/floor change yields a
    different cache key (and thus a miss) instead of serving stale rankings."""
    cfg = get_config()
    payload = {
        "weights": cfg.retrieval_weights,
        "weights_no_vector": cfg.retrieval_weights_no_vector,
        "lexical": cfg.lexical_weights,
        "halflife": cfg.freshness_half_life_hours,
        "pinned_bonus": cfg.pinned_bonus,
        "diversity_cap": cfg.session_diversity_cap,
        "rrf": [cfg.rrf_tiebreaker_enabled, cfg.rrf_tiebreaker_threshold],
        "floor_ratio": cfg.boost_floor_ratio,
        "profiles": cfg.retrieval_profiles,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]


def make_cache_key(query_text: str, params: dict[str, Any]) -> str:
    blob = {"q": query_text.strip(), "p": params, "cfg": config_fingerprint()}
    return hashlib.sha256(json.dumps(blob, sort_keys=True, default=str).encode()).hexdigest()


def _connect(db_path: str) -> sqlite3.Connection:
    return open_conn(db_path)


def current_generation(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT value FROM cache_meta WHERE key = 'corpus_generation'"
    ).fetchone()
    return int(row[0]) if row else 0


def read_generation(db_path: str) -> int:
    """Return the current corpus generation, or 0 on any error.

    Callers capture this BEFORE reading the corpus they are about to compute a
    result from, then pass it to ``write()`` — see the TOCTOU note there."""
    try:
        conn = _connect(db_path)
    except sqlite3.Error as exc:
        logger.warning("query_cache.read_generation connect failed: %s", exc)
        return 0
    try:
        return current_generation(conn)
    except (sqlite3.Error, ValueError) as exc:
        logger.warning("query_cache.read_generation failed: %s", exc)
        return 0
    finally:
        conn.close()


def read(db_path: str, cache_key: str) -> list[dict] | None:
    """Return cached result stubs if present AND still fresh (generation match)."""
    try:
        conn = _connect(db_path)
    except sqlite3.Error as exc:
        logger.warning("query_cache.read connect failed: %s", exc)
        return None
    try:
        gen = current_generation(conn)
        row = conn.execute(
            "SELECT result_json, generation FROM query_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
        if row is None:
            return None
        if int(row["generation"]) != gen:
            try:
                conn.execute("DELETE FROM query_cache WHERE cache_key = ?", (cache_key,))
                conn.commit()
            except sqlite3.Error:
                pass
            return None
        return json.loads(row["result_json"])
    except (sqlite3.Error, json.JSONDecodeError, ValueError) as exc:
        logger.warning("query_cache.read failed (cache disabled for this query): %s", exc)
        return None
    finally:
        conn.close()


def evict_stale(db_path: str) -> None:
    """Delete query cache rows older than the current corpus generation."""
    try:
        conn = _connect(db_path)
    except sqlite3.Error as exc:
        logger.warning("query_cache.evict_stale connect failed: %s", exc)
        return
    try:
        gen = current_generation(conn)
        conn.execute("DELETE FROM query_cache WHERE generation < ?", (gen,))
        conn.commit()
    except (sqlite3.Error, ValueError) as exc:
        logger.warning("query_cache.evict_stale failed: %s", exc)
    finally:
        conn.close()


def write(db_path: str, cache_key: str, stub_rows: list[dict], generation: int) -> None:
    """Store result stubs tagged with ``generation``. Best-effort.

    TOCTOU correctness: ``generation`` MUST be the corpus generation captured by
    the caller BEFORE it read the candidates it computed ``stub_rows`` from. If
    we re-read the generation here instead, a claim write that raced in between
    the caller's corpus read and this write would have bumped the counter, and
    we would tag a stale (generation-G) result as the new generation (G+1) — so
    a subsequent read at G+1 would serve the stale ranking, defeating the
    generation gate. Tagging with the compute-time generation guarantees any
    racing write correctly invalidates this entry. (audit: qc-generation-toctou)
    """
    try:
        conn = _connect(db_path)
    except sqlite3.Error as exc:
        logger.warning("query_cache.write connect failed: %s", exc)
        return
    try:
        conn.execute(
            """INSERT INTO query_cache (cache_key, result_json, generation, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(cache_key) DO UPDATE SET
                   result_json = excluded.result_json,
                   generation = excluded.generation,
                   created_at = excluded.created_at""",
            (cache_key, json.dumps(stub_rows), generation, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    except (sqlite3.Error, TypeError, ValueError) as exc:
        logger.warning("query_cache.write failed (result not cached): %s", exc)
    finally:
        conn.close()

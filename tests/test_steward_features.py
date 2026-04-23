"""Unit tests for the v1 steward classifier feature extractor."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from memorymaster.steward_features import (
    FEATURE_KEYS,
    FEATURE_VERSION,
    extract_features,
    feature_vector,
)

_SCHEMA = """
CREATE TABLE claims (id INTEGER PRIMARY KEY, text TEXT, subject TEXT,
    predicate TEXT, object_value TEXT, scope TEXT, status TEXT,
    claim_type TEXT, source_agent TEXT, created_at TEXT,
    access_count INTEGER DEFAULT 0);
CREATE TABLE citations (id INTEGER PRIMARY KEY, claim_id INTEGER,
    source TEXT, excerpt TEXT);
CREATE TABLE events (id INTEGER PRIMARY KEY, claim_id INTEGER,
    event_type TEXT, details TEXT, created_at TEXT);
"""


def _iso_ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.executescript(_SCHEMA)
    c.commit()
    return c


def _ins(conn: sqlite3.Connection, **overrides) -> int:
    d = {"text": "x", "subject": "foo", "predicate": "requires", "object_value": "bar",
         "scope": "project:memorymaster", "status": "candidate", "claim_type": "decision",
         "source_agent": "claude-session", "created_at": _iso_ago(3), "access_count": 2}
    d.update(overrides)
    cols = ", ".join(d.keys())
    ph = ", ".join("?" for _ in d)
    cur = conn.execute(f"INSERT INTO claims ({cols}) VALUES ({ph})", tuple(d.values()))
    conn.commit()
    return int(cur.lastrowid)


def test_feature_version_is_stable() -> None:
    assert FEATURE_VERSION == "v1"
    assert len(FEATURE_KEYS) == 9
    assert "n_citations" in FEATURE_KEYS


def test_n_citations_counts_rows(conn) -> None:
    cid = _ins(conn)
    conn.executemany("INSERT INTO citations (claim_id, source, excerpt) VALUES (?, ?, ?)",
                     [(cid, "s1", "v"), (cid, "s2", None), (cid, "s3", "")])
    assert extract_features({"id": cid, "scope": "project:x"}, conn)["n_citations"] == 3.0


def test_source_agent_trust(conn) -> None:
    cid = _ins(conn)
    f = lambda sa: extract_features({"id": cid, "source_agent": sa}, conn)["source_agent_trust"]  # noqa: E731
    assert f("claude-session") == 1.0
    assert f("random-bot") == pytest.approx(0.3)
    assert f(None) == pytest.approx(0.1)


def test_scope_quality_binning(conn) -> None:
    cid = _ins(conn)
    f = lambda s: extract_features({"id": cid, "scope": s}, conn)["scope_quality"]  # noqa: E731
    assert f("global") == 1.0
    assert f("project:memorymaster") == pytest.approx(0.8)
    assert f("project") == pytest.approx(0.2)  # bare fallback, red flag
    assert f(None) == 0.0


def test_conflict_delta_counts_disagreement(conn) -> None:
    cid = _ins(conn, subject="x", predicate="is", object_value="A", scope="project:z")
    for obj in ("A", "B", "C"):
        _ins(conn, subject="x", predicate="is", object_value=obj, scope="project:z", status="confirmed")
    feats = extract_features(
        {"id": cid, "subject": "x", "predicate": "is", "object_value": "A", "scope": "project:z"},
        conn,
    )
    assert feats["conflict_delta"] == 1.0  # 2 conflicts - 1 agreement


def test_session_age_days_recent_vs_old(conn) -> None:
    recent = _ins(conn, created_at=_iso_ago(0.5))
    old = _ins(conn, created_at=_iso_ago(30))
    assert extract_features({"id": recent, "created_at": _iso_ago(0.5)}, conn)["session_age_days"] < 1.0
    assert extract_features({"id": old, "created_at": _iso_ago(30)}, conn)["session_age_days"] >= 29.0


def test_access_count_passthrough(conn) -> None:
    cid = _ins(conn)
    assert extract_features({"id": cid, "access_count": 42}, conn)["access_count"] == 42.0


def test_has_verbatim_excerpt(conn) -> None:
    a = _ins(conn)
    b = _ins(conn)
    conn.execute("INSERT INTO citations (claim_id, source, excerpt) VALUES (?, ?, ?)", (a, "s", "real"))
    conn.execute("INSERT INTO citations (claim_id, source, excerpt) VALUES (?, ?, ?)", (b, "s", None))
    conn.commit()
    assert extract_features({"id": a}, conn)["has_verbatim_excerpt"] == 1.0
    assert extract_features({"id": b}, conn)["has_verbatim_excerpt"] == 0.0


def test_claim_type_bin(conn) -> None:
    cid = _ins(conn)
    for ct, expected in [("bug", 1.0), ("decision", 2.0), ("constraint", 3.0),
                         ("reference", 4.0), ("unknown", 0.0)]:
        assert extract_features({"id": cid, "claim_type": ct}, conn)["claim_type_bin"] == expected


def test_sensitivity_flagged(conn) -> None:
    flagged = _ins(conn)
    clean = _ins(conn)
    conn.execute(
        "INSERT INTO events (claim_id, event_type, details, created_at) VALUES (?, ?, ?, ?)",
        (flagged, "audit", "sensitivity filter blocked", _iso_ago(1)),
    )
    conn.commit()
    assert extract_features({"id": flagged}, conn)["sensitivity_flagged"] == 1.0
    assert extract_features({"id": clean}, conn)["sensitivity_flagged"] == 0.0


def test_feature_vector_preserves_order(conn) -> None:
    cid = _ins(conn)
    feats = extract_features({"id": cid}, conn)
    vec = feature_vector(feats)
    assert len(vec) == len(FEATURE_KEYS)
    assert vec[FEATURE_KEYS.index("n_citations")] == feats["n_citations"]


def test_accepts_dataclass(conn) -> None:
    from dataclasses import dataclass

    @dataclass
    class MC:
        id: int
        subject: str
        predicate: str
        object_value: str
        scope: str
        created_at: str
        source_agent: str
        claim_type: str
        access_count: int

    cid = _ins(conn)
    obj = MC(cid, "foo", "requires", "bar", "project:x", _iso_ago(2),
             "claude-session", "decision", 3)
    feats = extract_features(obj, conn)
    assert feats["source_agent_trust"] == 1.0
    assert feats["claim_type_bin"] == 2.0
    assert feats["access_count"] == 3.0

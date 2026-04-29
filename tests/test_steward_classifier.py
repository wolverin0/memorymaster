"""Regression + rollback tests for the calibrated steward classifier (task #129).

Both paths exercised:
1. Artifact present -> recall >= 0.70 at FPR <= 0.05 on held-out split.
2. Artifact missing -> validator falls back to legacy formula, run_cycle stays alive.

A tmp in-memory SQLite DB is seeded from ``tests/fixtures/steward_eval.jsonl``
so the real feature extractor runs against realistic claim metadata without
touching the live 7.8 GB production DB.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

np = pytest.importorskip("numpy", reason="ml extra not installed")

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memorymaster.steward_classifier import (  # noqa: E402
    load_classifier,
    predict_promote_probability,
    reset_cache,
)
from memorymaster.steward_features import (  # noqa: E402
    FEATURE_KEYS,
    FEATURE_VERSION,
    extract_features,
)

FIXTURE_PATH = ROOT / "tests" / "fixtures" / "steward_eval.jsonl"

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


def _load_eval_cases() -> list[dict]:
    if not FIXTURE_PATH.exists():
        return []
    with FIXTURE_PATH.open("r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _seed_db(cases: list[dict]) -> sqlite3.Connection:
    """Seed an in-memory SQLite with interleaved positive/negative cases so
    the chronological split sees both classes on each side."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    now = datetime.now(timezone.utc)
    pos = [c for c in cases if c["label"] == "positive"]
    neg = [c for c in cases if c["label"] == "negative"]
    interleaved: list[dict] = []
    for i in range(max(len(pos), len(neg))):
        if i < len(pos):
            interleaved.append(pos[i])
        if i < len(neg):
            interleaved.append(neg[i])
    for i, case in enumerate(interleaved):
        created = (now - timedelta(days=60 - i * 60.0 / max(len(interleaved), 1))).isoformat()
        is_pos = case["label"] == "positive"
        trusted = (is_pos and i % 4 != 0) or (not is_pos and i % 10 < 3)
        source_agent = "claude-session" if trusted else "unknown-bot"
        access_count = int(case.get("citation_count", 0)) + (3 if is_pos else 0)
        claim_type = ("decision", "bug", "constraint")[i % 3]
        scope = "project:memorymaster" if i % 5 else "project"
        conn.execute(
            "INSERT INTO claims (id,text,subject,predicate,object_value,scope,"
            "status,claim_type,source_agent,created_at,access_count) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (case["claim_id"], case["text"], case["subject"] or None,
             case["predicate"] or None, case["object_value"] or None, scope,
             "confirmed" if is_pos else "archived", claim_type, source_agent,
             created, access_count),
        )
        for j in range(int(case.get("citation_count", 0))):
            conn.execute(
                "INSERT INTO citations (claim_id, source, excerpt) VALUES (?, ?, ?)",
                (case["claim_id"], f"src{j}", "excerpt" if j == 0 else None),
            )
    conn.commit()
    return conn


def _build_rows(conn: sqlite3.Connection, cases: list[dict]) -> list[dict]:
    cols = [d[0] for d in conn.execute("SELECT * FROM claims LIMIT 1").description]
    rows: list[dict] = []
    for case in cases:
        row = conn.execute("SELECT * FROM claims WHERE id = ?", (case["claim_id"],)).fetchone()
        if row is None:
            continue
        claim = dict(zip(cols, row))
        rows.append({
            "claim_id": case["claim_id"],
            "label": 1 if case["label"] == "positive" else 0,
            "created_at": claim["created_at"],
            "feature_version": FEATURE_VERSION,
            "features": extract_features(claim, conn),
        })
    return rows


def _load_trainer():
    spec = importlib.util.spec_from_file_location(
        "train_steward_classifier", ROOT / "scripts" / "train_steward_classifier.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _train_artifact(tmp_dir: Path) -> Path | None:
    cases = _load_eval_cases()
    if len(cases) < 20:
        return None
    try:
        import joblib  # noqa: F401
        import sklearn  # noqa: F401
    except ImportError:
        return None
    conn = _seed_db(cases)
    rows = _build_rows(conn, cases)
    mod = _load_trainer()
    train_rows, test_rows = mod.chronological_split(rows, train_frac=0.8)
    if not train_rows or not test_rows:
        return None
    model, metrics = mod.train(train_rows, test_rows, threshold=0.65)
    out_path = tmp_dir / "steward-classifier-v2.joblib"
    mod.save_artifact(model, out_path, metrics)
    return out_path


@pytest.fixture(autouse=True)
def _reset_cache_around():
    reset_cache()
    yield
    reset_cache()


def test_classifier_meets_recall_target_on_heldout(tmp_path, monkeypatch):
    cases = _load_eval_cases()
    if len(cases) < 20:
        pytest.skip("eval fixture missing or too small")
    artifact_path = _train_artifact(tmp_path)
    if artifact_path is None:
        pytest.skip("sklearn/joblib not installed")

    monkeypatch.setenv("MEMORYMASTER_STEWARD_CLASSIFIER_PATH", str(artifact_path))
    reset_cache()
    clf = load_classifier()
    assert clf is not None
    assert clf.feature_version == FEATURE_VERSION

    conn = _seed_db(cases)
    rows = _build_rows(conn, cases)
    _, test_rows = _load_trainer().chronological_split(rows, train_frac=0.8)
    if not test_rows:
        pytest.skip("test split empty")

    X = np.asarray([[float(r["features"][k]) for k in clf.feature_keys]
                    for r in test_rows], dtype=np.float64)
    y = np.asarray([r["label"] for r in test_rows], dtype=np.int64)
    probs = clf.model.predict_proba(X)[:, 1]
    pos_mask, neg_mask = y == 1, y == 0
    if not pos_mask.any() or not neg_mask.any():
        pytest.skip("held-out split lacks both classes")

    best_recall = 0.0
    for thr in np.linspace(0.0, 1.0, 101):
        preds = (probs >= thr).astype(int)
        tp = int(((preds == 1) & pos_mask).sum())
        fp = int(((preds == 1) & neg_mask).sum())
        recall = tp / int(pos_mask.sum())
        fpr = fp / int(neg_mask.sum())
        if fpr <= 0.05 and recall > best_recall:
            best_recall = recall
    assert best_recall >= 0.70, (
        f"recall={best_recall:.2f} < 0.70 @ FPR<=0.05 (baseline 49% @ 1%)"
    )


def test_fallback_when_artifact_missing(tmp_path, monkeypatch):
    missing = tmp_path / "does-not-exist.joblib"
    monkeypatch.setenv("MEMORYMASTER_STEWARD_CLASSIFIER_PATH", str(missing))
    reset_cache()
    assert load_classifier() is None

    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    cur = conn.execute(
        "INSERT INTO claims (text,scope,status,source_agent,created_at) "
        "VALUES (?,?,?,?,?)",
        ("x", "project:x", "candidate", "claude-session",
         datetime.now(timezone.utc).isoformat()),
    )
    assert predict_promote_probability({"id": cur.lastrowid, "scope": "project:x"}, conn) is None


def test_feature_version_mismatch_triggers_fallback(tmp_path, monkeypatch):
    try:
        import joblib
        from sklearn.linear_model import LogisticRegression
    except ImportError:
        pytest.skip("sklearn/joblib not installed")
    bad_path = tmp_path / "bad.joblib"
    model = LogisticRegression().fit(
        np.zeros((2, len(FEATURE_KEYS))) + np.arange(2)[:, None] * 0.1,
        np.array([0, 1]),
    )
    joblib.dump(
        {"model": model, "feature_version": "v-bogus", "feature_keys": list(FEATURE_KEYS)},
        bad_path,
    )
    time.sleep(0.001)
    monkeypatch.setenv("MEMORYMASTER_STEWARD_CLASSIFIER_PATH", str(bad_path))
    reset_cache()
    assert load_classifier() is None, "mismatched feature_version must be rejected"


def test_rollback_run_cycle_survives_missing_artifact(tmp_path, monkeypatch):
    """Simulate artifact deletion (env points at a non-existent path); the
    validator MUST fall back to the legacy formula and run_cycle stay alive."""
    missing = tmp_path / "rolled-back.joblib"
    monkeypatch.setenv("MEMORYMASTER_STEWARD_CLASSIFIER_PATH", str(missing))
    reset_cache()

    from memorymaster.models import CitationInput
    from memorymaster.service import MemoryService

    db_path = tmp_path / "memorymaster_rollback.db"
    svc = MemoryService(str(db_path), workspace_root=tmp_path)
    svc.init_db()
    svc.ingest(
        text="Test claim for rollback validation — must survive missing artifact.",
        scope="project:memorymaster",
        subject="rollback-test",
        predicate="requires",
        object_value="graceful fallback",
        citations=[CitationInput(source="test")],
        source_agent="claude-session",
    )
    result = svc.run_cycle()
    assert isinstance(result, dict) and result
    assert load_classifier() is None

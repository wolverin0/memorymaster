"""F-20: scheduled archival filters in SQL and needs a recorded S1 judgment.

Review addendum F-20: ``scheduled_archive`` loaded the first 500 stale claims
ordered by confidence and only then filtered ``access_count == 0`` and age, so
it was dormant by accident (0 eligible in the first 500; 266 by another order).
Fixing the filter alone would archive thousands of claims irreversibly by
clock.  Contract: filter in SQL *and* archive only claims with a recorded live
S1 (REVALIDATE) ``no_longer_useful`` judgment in the decisions ledger; an
absent ledger or judgment archives nothing.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from memorymaster.core import lifecycle
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.govern.jobs import scheduled_archive
from memorymaster.recall import qdrant_outbox

LEDGER_DDL = """
CREATE TABLE decisions(decision_id TEXT PRIMARY KEY, ts TEXT, surface TEXT, mode TEXT,
  policy_version TEXT, question_set_id TEXT, question_sha256 TEXT, primitive_summary TEXT,
  model_requested TEXT, model_served TEXT, backend TEXT, transport_version TEXT, sdk_version TEXT,
  code_revision TEXT, state_schema_version INT, state_sha256 TEXT, state_redacted TEXT,
  egress_bytes INT, redaction_counts_json TEXT, transport_outcome TEXT, fallback_reason TEXT,
  latency_ms INT, attempt_count INT, tokens_in INT, tokens_out INT, cost_usd REAL,
  legacy_action TEXT, jev_action TEXT, available_actions_json TEXT, action_taken TEXT,
  exploration_arm TEXT, action_propensities_json TEXT, chosen_propensity REAL, randomization_id TEXT,
  thresholds_json TEXT, baseline_features_json TEXT, session_key TEXT, scope TEXT, tenant TEXT);
CREATE TABLE decision_items(decision_id TEXT, item_ref TEXT, item_kind TEXT, question_id TEXT,
  question_version INT, answer TEXT, probabilities_json TEXT, confidence REAL, rank_legacy INT,
  rank_final INT, exposed INT, delivered INT, PRIMARY KEY(decision_id, item_ref, question_id));
"""

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _iso(delta_days: float = 0.0) -> str:
    return (NOW + timedelta(days=delta_days)).isoformat()


def _engine_json(value: object) -> str:
    """How ``decisions.engine`` stores ``legacy_action``/``jev_action``/``action_taken``."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def record_judgment(ledger: Path, claim_id: int, *, action: str = "no_longer_useful",
                    mode: str = "live", fallback_reason: str | None = None,
                    ts: str | None = None, surface: str = "revalidate",
                    item_ref: str | None = None, engine_shape: bool = True) -> None:
    """Write one S1 decision the way ``engine.decide`` does (lowercase surface,
    JSON-encoded actions); ``engine_shape=False`` writes raw action strings."""
    fresh = not ledger.exists()
    conn = sqlite3.connect(ledger)
    if fresh:
        conn.executescript(LEDGER_DDL)
    decision_id = uuid.uuid4().hex
    encode = _engine_json if engine_shape else str
    taken = action if mode == "live" and not fallback_reason else "keep_stale"
    conn.execute(
        "INSERT INTO decisions(decision_id, ts, surface, mode, fallback_reason, legacy_action, "
        "jev_action, action_taken) VALUES (?,?,?,?,?,?,?,?)",
        (decision_id, ts or _iso(), surface, mode, fallback_reason, encode("keep_stale"),
         encode(action), encode(taken)),
    )
    for question in ("lifecycle.still_valid", "lifecycle.durable", "lifecycle.useful_future"):
        conn.execute(
            "INSERT INTO decision_items(decision_id, item_ref, item_kind, question_id, "
            "question_version, answer) VALUES (?,?,?,?,?,?)",
            (decision_id, item_ref or str(claim_id), "claim", question, 1, "no"),
        )
    conn.commit()
    conn.close()


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(qdrant_outbox.ENV_OUTBOX_DIR, str(tmp_path / "qdrant-outbox"))
    monkeypatch.delenv("QDRANT_URL", raising=False)
    ledger = tmp_path / "decisions.db"
    monkeypatch.setenv("MEMORYMASTER_DECISIONS_DB", str(ledger))
    service = MemoryService(tmp_path / "archive.db", workspace_root=tmp_path)
    service.init_db()
    return service, ledger


def _stale(service, text: str, *, age_days: int = 30, access_count: int = 0,
           confidence: float = 0.5, pinned: bool = False) -> int:
    claim = service.ingest(text, [CitationInput(source="test://scheduled-archive")],
                           confidence=confidence)
    lifecycle.transition_claim(service.store, claim.id, "confirmed", reason="fixture",
                               event_type="validator")
    lifecycle.transition_claim(service.store, claim.id, "stale", reason="fixture",
                               event_type="decay")
    old = _iso(-age_days)
    with service.store.connect() as conn:
        conn.execute(
            "UPDATE claims SET created_at=?, updated_at=?, access_count=?, pinned=?, confidence=? "
            "WHERE id=?",
            (old, old, access_count, int(pinned), confidence, claim.id),
        )
        conn.commit()
    return claim.id


def _status(service, claim_id: int) -> str:
    return service.store.get_claim(claim_id, include_citations=False).status


def test_absent_ledger_archives_nothing(env):
    service, ledger = env
    claim_id = _stale(service, "stale unused claim without any judgment")
    assert not ledger.exists()

    result = scheduled_archive.run(service, older_than_days=14)

    assert result["archived"] == 0
    assert _status(service, claim_id) == "stale"
    assert not ledger.exists(), "the gate must never create the ledger"


def test_absent_ledger_does_not_page_the_stale_backlog(env, monkeypatch):
    # Verifier note: with no ledger every 6-hourly run paged through the whole
    # SQL-eligible stale backlog (~18 pages of 500 in production) to archive 0.
    service, ledger = env
    _stale(service, "stale unused claim without any ledger")
    pages = []
    real = service.store.find_archive_candidates
    monkeypatch.setattr(service.store, "find_archive_candidates",
                        lambda **kwargs: pages.append(kwargs) or real(**kwargs))

    result = scheduled_archive.run(service, older_than_days=14)

    assert pages == []
    assert result["archived"] == 0
    assert not ledger.exists()


def test_only_claims_judged_no_longer_useful_are_archived(env):
    service, ledger = env
    judged = _stale(service, "stale claim judged no longer useful by S1")
    unjudged = _stale(service, "stale claim nobody judged yet")
    record_judgment(ledger, judged)

    result = scheduled_archive.run(service, older_than_days=14)

    assert result["archived"] == 1
    assert result["unjudged"] == 1
    assert _status(service, judged) == "archived"
    assert _status(service, unjudged) == "stale"


def test_eligibility_is_filtered_in_sql_before_the_batch_limit(env):
    """600 accessed high-confidence stale rows must not hide the eligible one."""
    service, ledger = env
    seed = _stale(service, "accessed stale noise", access_count=5, confidence=0.99)
    conn = sqlite3.connect(service.store.db_path)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(claims)") if r[1] != "id"]
    select = [
        "text || '-' || n" if col == "text"
        else "NULL" if col in ("idempotency_key", "human_id")
        else "'subj-' || n" if col == "subject"
        else col
        for col in cols
    ]
    conn.execute(
        "WITH RECURSIVE s(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM s WHERE n < 600) "
        f"INSERT INTO claims ({','.join(cols)}) SELECT {','.join(select)} "
        "FROM claims, s WHERE claims.id = ?",
        (seed,),
    )
    conn.commit()
    conn.close()
    target = _stale(service, "never accessed stale claim", confidence=0.1)
    record_judgment(ledger, target)

    result = scheduled_archive.run(service, older_than_days=14, limit=500)

    assert result["archived"] == 1
    assert _status(service, target) == "archived"
    assert _status(service, seed) == "stale"


@pytest.mark.parametrize(
    "judgment",
    [
        {"mode": "shadow"},
        {"fallback_reason": "timeout"},
        {"action": "keep_stale"},
        {"surface": "ingest"},
    ],
    ids=["shadow", "fallback", "other-verdict", "other-surface"],
)
def test_non_live_or_other_judgments_do_not_archive(env, judgment):
    service, ledger = env
    claim_id = _stale(service, "stale claim with a non-authorizing decision")
    record_judgment(ledger, claim_id, **judgment)

    assert scheduled_archive.run(service, older_than_days=14)["archived"] == 0
    assert _status(service, claim_id) == "stale"


def test_later_verdict_revokes_an_earlier_no_longer_useful(env):
    service, ledger = env
    claim_id = _stale(service, "stale claim judged twice")
    record_judgment(ledger, claim_id, ts=_iso(-2))
    record_judgment(ledger, claim_id, action="keep_stale", ts=_iso(-1))

    assert scheduled_archive.run(service, older_than_days=14)["archived"] == 0
    assert _status(service, claim_id) == "stale"


def test_judgment_older_than_the_claims_last_change_does_not_archive(env):
    service, ledger = env
    claim_id = _stale(service, "stale claim revalidated after its judgment", age_days=30)
    record_judgment(ledger, claim_id, ts=_iso(-40))

    assert scheduled_archive.run(service, older_than_days=14)["archived"] == 0
    assert _status(service, claim_id) == "stale"


def test_pinned_and_young_claims_are_never_candidates(env):
    service, ledger = env
    pinned = _stale(service, "pinned stale claim", pinned=True)
    young = _stale(service, "recent stale claim", age_days=3)
    for claim_id in (pinned, young):
        record_judgment(ledger, claim_id)

    assert scheduled_archive.run(service, older_than_days=14)["archived"] == 0
    assert _status(service, pinned) == "stale"
    assert _status(service, young) == "stale"


def test_hand_written_uppercase_raw_judgment_is_still_accepted(env):
    """Backward compatibility: ``REVALIDATE`` + raw ``no_longer_useful`` also counts."""
    service, ledger = env
    claim_id = _stale(service, "stale claim judged by a raw ledger row")
    record_judgment(ledger, claim_id, surface="REVALIDATE", engine_shape=False)

    assert scheduled_archive.run(service, older_than_days=14)["archived"] == 1
    assert _status(service, claim_id) == "archived"


def test_prefixed_item_ref_is_accepted(env):
    service, ledger = env
    claim_id = _stale(service, "stale claim referenced as claim:<id>")
    record_judgment(ledger, claim_id, item_ref=f"claim:{claim_id}")

    assert scheduled_archive.run(service, older_than_days=14)["archived"] == 1


def test_postgres_archive_candidates_sql_shape():
    """No DSN in CI: pin the Postgres query shape (``%s`` placeholders, boolean
    ``pinned``, keyset paging) and bind a native datetime for TIMESTAMPTZ."""
    from memorymaster.stores.postgres_store import PostgresStore

    executed: list[tuple[str, list[object]]] = []

    class _Cursor:
        def execute(self, sql, params=None):
            executed.append((" ".join(sql.split()), list(params or [])))

        def fetchall(self):
            return []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class _Conn:
        def cursor(self):
            return _Cursor()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    store = PostgresStore.__new__(PostgresStore)
    store.tenant_id = None
    store.connect = lambda: _Conn()  # type: ignore[method-assign]

    cutoff = "2026-09-09T12:00:00+00:00"
    assert store.find_archive_candidates(created_before=cutoff, after_id=41, limit=7) == []

    sql, params = executed[-1]
    for clause in ("status = 'stale'", "access_count = 0", "pinned = FALSE",
                   "created_at < %s", "id > %s", "ORDER BY id ASC", "LIMIT %s"):
        assert clause in sql
    assert "?" not in sql
    assert params == [datetime(2026, 9, 9, 12, tzinfo=timezone.utc), 41, 7]
    assert isinstance(params[0], datetime) and params[0].tzinfo is not None

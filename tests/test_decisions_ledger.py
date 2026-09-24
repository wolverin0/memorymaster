"""Decision ledger: schema v1, WAL, append-only, multi-process appends, prune, never raises."""
from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from memorymaster.decisions import ledger as ld
from memorymaster.decisions.ledger import DecisionLedger, DecisionRecord, ItemRecord, OutcomeRecord
from memorymaster.decisions.questions import get as get_question

REPO = Path(__file__).resolve().parents[1]


def _decision(decision_id: str = "d1", **overrides) -> DecisionRecord:
    base = dict(decision_id=decision_id, surface="recall", mode="live", state_redacted='{"request":"x"}',
                state_sha256="ab" * 32, transport_outcome="ok", cost_usd=0.001, attempt_count=1,
                legacy_action='["claim:1"]', action_taken='["claim:1"]', session_key="s1")
    base.update(overrides)
    return DecisionRecord(**base)


def _item(decision_id: str = "d1", item_ref: str = "claim:1", question_id: str = "recall.usable_evidence", **kw):
    base = dict(decision_id=decision_id, item_ref=item_ref, item_kind="claim", question_id=question_id,
                question_version=1, answer="0.8", probabilities_json='{"noul":0.8}', confidence=None,
                rank_legacy=1, rank_final=1, exposed=1, delivered=1)
    base.update(kw)
    return ItemRecord(**base)


@pytest.fixture()
def ledger(tmp_path):
    return DecisionLedger(tmp_path / "decisions.db")


def test_schema_v1_wal_and_tables(ledger, tmp_path):
    assert ledger.write_decision(_decision(), [_item()]) is True
    conn = sqlite3.connect(tmp_path / "decisions.db")
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"decisions", "decision_items", "outcomes", "question_versions", "watermarks"} <= tables
        columns = [r[1] for r in conn.execute("PRAGMA table_info(decisions)")]
        assert columns == list(ld.DECISION_COLUMNS)
        item_cols = [r[1] for r in conn.execute("PRAGMA table_info(decision_items)")]
        assert item_cols == list(ld.ITEM_COLUMNS)
        outcome_cols = [r[1] for r in conn.execute("PRAGMA table_info(outcomes)")]
        assert outcome_cols == ["outcome_id", *ld.OUTCOME_COLUMNS]
    finally:
        conn.close()


def test_contract_decision_columns():
    assert ld.DECISION_COLUMNS == (
        "decision_id", "ts", "surface", "mode", "policy_version", "question_set_id", "question_sha256",
        "primitive_summary", "model_requested", "model_served", "backend", "transport_version", "sdk_version",
        "code_revision", "state_schema_version", "state_sha256", "state_redacted", "egress_bytes",
        "redaction_counts_json", "transport_outcome", "fallback_reason", "latency_ms", "engine_ms",
        "attempt_count",
        "tokens_in", "tokens_out", "cost_usd", "legacy_action", "jev_action", "available_actions_json",
        "action_taken", "exploration_arm", "action_propensities_json", "chosen_propensity", "randomization_id",
        "thresholds_json", "baseline_features_json", "session_key", "scope", "tenant",
    )


def test_round_trip_and_items(ledger):
    assert ledger.write_decision(_decision(), [_item(), _item(item_ref="claim:2", rank_final=2, exposed=0)])
    rows = ledger.query("SELECT * FROM decisions")
    assert len(rows) == 1 and rows[0]["surface"] == "recall" and rows[0]["ts"]
    items = ledger.query("SELECT item_ref, exposed FROM decision_items ORDER BY item_ref")
    assert [(r["item_ref"], r["exposed"]) for r in items] == [("claim:1", 1), ("claim:2", 0)]


def test_duplicate_decision_is_counted_not_raised(ledger):
    before = ld.ledger_write_failures()
    assert ledger.write_decision(_decision(), [_item()]) is True
    assert ledger.write_decision(_decision(surface="dedup"), [_item()]) is False
    assert ld.ledger_write_failures() == before + 1
    assert ledger.write_failures == 1
    assert ledger.query("SELECT surface FROM decisions")[0]["surface"] == "recall"


def test_decisions_are_append_only_at_the_database(ledger, tmp_path):
    ledger.write_decision(_decision(), [_item()])
    ledger.record_outcomes([OutcomeRecord("d1", "claim:1", "used_in_turn", 1.0, observed_at="2026-09-23T00:00:00+00:00")])
    conn = sqlite3.connect(tmp_path / "decisions.db")
    try:
        for sql in ("UPDATE decisions SET action_taken='x'", "DELETE FROM decisions",
                    "UPDATE decision_items SET exposed=0", "DELETE FROM decision_items",
                    "UPDATE outcomes SET value=0", "DELETE FROM outcomes",
                    "UPDATE decisions SET state_redacted='other'"):
            with pytest.raises(sqlite3.DatabaseError):
                conn.execute(sql)
        conn.execute("UPDATE decisions SET state_redacted=NULL")  # the prune path stays allowed
        conn.commit()
    finally:
        conn.close()


def test_write_failure_never_raises_and_logs_once(tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(ld, "_logged", False)  # "once" is per process; earlier tests may have logged
    blocker = tmp_path / "not-a-db"
    blocker.mkdir()
    broken = DecisionLedger(blocker)
    with caplog.at_level(logging.WARNING, logger="memorymaster.decisions.ledger"):
        assert broken.write_decision(_decision("x1"), []) is False
        assert broken.write_decision(_decision("x2"), []) is False
        assert broken.record_outcomes([OutcomeRecord("x1", "", "late_answer", 1.0)]) == 0
        assert broken.set_watermark("w", "1") is False
    assert broken.write_failures == 4
    assert sum("decision ledger" in r.getMessage() for r in caplog.records) == 1
    assert broken.get_watermark("w") is None
    assert broken.spend_since("2026-01-01T00:00:00+00:00") is None


def test_outcomes_are_idempotent_and_do_not_touch_decisions(ledger):
    ledger.write_decision(_decision(), [_item()])
    outcome = OutcomeRecord("d1", "claim:1", "used_in_turn", 1.0, was_exposed=1, label_source="detector",
                            observed_at="2026-09-23T01:00:00+00:00", lag_s=30)
    assert ledger.record_outcomes([outcome, outcome]) == 1
    assert ledger.record_outcomes([outcome]) == 0
    assert len(ledger.query("SELECT * FROM outcomes")) == 1
    assert ledger.query("SELECT action_taken FROM decisions")[0]["action_taken"] == '["claim:1"]'


def test_question_versions_and_thresholds(ledger):
    spec = get_question("recall.usable_evidence")
    assert ledger.register_questions([spec]) is True
    assert ledger.register_questions([spec]) is True  # idempotent
    assert ledger.thresholds(spec.id, spec.version) == {"include": 0.5, "k_min": 2, "k_max": 5}
    row = ledger.query("SELECT sha256, primitive FROM question_versions")[0]
    assert row["sha256"] == spec.sha256 and row["primitive"] == "noul"
    assert ledger.thresholds("nope", 1) is None


def test_watermarks(ledger):
    assert ledger.get_watermark("lifecycle_events") is None
    assert ledger.set_watermark("lifecycle_events", "41") is True
    assert ledger.set_watermark("lifecycle_events", "42") is True
    assert ledger.get_watermark("lifecycle_events") == "42"


def test_prune_nulls_old_state_but_keeps_hashes(ledger):
    old = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    ledger.write_decision(_decision("old", ts=old), [])
    ledger.write_decision(_decision("new"), [])
    assert ledger.prune_state(180) == 1
    rows = {r["decision_id"]: r for r in ledger.query("SELECT * FROM decisions")}
    assert rows["old"]["state_redacted"] is None and rows["old"]["state_sha256"] == "ab" * 32
    assert rows["new"]["state_redacted"] == '{"request":"x"}'
    assert ledger.prune_state(180) == 0


def test_spend_and_request_counters(ledger):
    now = datetime.now(timezone.utc)
    ledger.write_decision(_decision("a", cost_usd=0.5), [])
    ledger.write_decision(_decision("b", cost_usd=0.25, attempt_count=0), [])
    ledger.write_decision(_decision("c", cost_usd=9.0, ts=(now - timedelta(days=2)).isoformat()), [])
    since = (now - timedelta(hours=1)).isoformat()
    assert ledger.spend_since(since) == pytest.approx(0.75)
    assert ledger.requests_since(since) == 1


def test_newer_schema_is_refused_without_raising_on_writes(tmp_path):
    path = tmp_path / "future.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 7")
    conn.commit()
    conn.close()
    ledger = DecisionLedger(path)
    assert ledger.write_decision(_decision(), []) is False
    with pytest.raises(ld.LedgerReadError):  # refused is not the same as empty
        ledger.query("SELECT 1 AS x")


_WORKER = textwrap.dedent(
    """
    import sys
    from memorymaster.decisions.ledger import DecisionLedger, DecisionRecord, ItemRecord
    path, worker, count = sys.argv[1], sys.argv[2], int(sys.argv[3])
    ledger = DecisionLedger(path)
    ok = 0
    for n in range(count):
        did = f"w{worker}-{n}"
        rec = DecisionRecord(decision_id=did, surface="recall", mode="live", session_key=f"s{worker}")
        items = [ItemRecord(decision_id=did, item_ref=f"claim:{k}", item_kind="claim", question_id="q",
                            question_version=1, exposed=1) for k in range(3)]
        ok += ledger.write_decision(rec, items)
    print(ok, ledger.write_failures)
    """
)


def test_concurrent_processes_append_without_loss(tmp_path):
    path = tmp_path / "shared.db"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MEMORYMASTER_", "TYPESAFE_"))}
    env["PYTHONPATH"] = str(REPO)
    workers, per_worker = 8, 150
    procs = [
        subprocess.Popen([sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {str(REPO)!r})\n" + _WORKER,
                          str(path), str(w), str(per_worker)],
                         cwd=str(tmp_path), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for w in range(workers)
    ]
    results = [p.communicate(timeout=240) for p in procs]
    for proc, (out, err) in zip(procs, results):
        assert proc.returncode == 0, err
        written, failures = map(int, out.split())
        assert written == per_worker and failures == 0
    ledger = DecisionLedger(path)
    assert ledger.query("SELECT COUNT(*) AS n FROM decisions")[0]["n"] == workers * per_worker
    assert ledger.query("SELECT COUNT(*) AS n FROM decision_items")[0]["n"] == workers * per_worker * 3


def test_prune_job_uses_configured_retention_and_never_creates_a_ledger(tmp_path):
    from memorymaster.decisions.config import DecisionConfig

    missing = DecisionConfig.from_env({"MEMORYMASTER_DECISIONS_DB": str(tmp_path / "none.db")})
    assert ld.prune_job(missing) == 0
    assert not (tmp_path / "none.db").exists()
    config = DecisionConfig.from_env({"MEMORYMASTER_DECISIONS_DB": str(tmp_path / "d.db"),
                                      "MEMORYMASTER_DECISIONS_STATE_RETENTION_DAYS": "30"})
    ledger = DecisionLedger(tmp_path / "d.db")
    ledger.write_decision(_decision("old", ts=(datetime.now(timezone.utc) - timedelta(days=45)).isoformat()), [])
    ledger.write_decision(_decision("new", ts=(datetime.now(timezone.utc) - timedelta(days=10)).isoformat()), [])
    assert ld.prune_job(config) == 1
    kept = {r["decision_id"]: r["state_redacted"] for r in ledger.query("SELECT * FROM decisions")}
    assert kept == {"old": None, "new": '{"request":"x"}'}


def test_open_failures_fail_fast_then_cool_down(tmp_path, monkeypatch):
    import time as _time

    calls = []

    def failing_open(*args, **kwargs):
        calls.append(1)
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(ld, "open_conn", failing_open)
    ledger = DecisionLedger(tmp_path / "flaky.db")
    started = _time.monotonic()
    for n in range(5):
        assert ledger.write_decision(_decision(f"z{n}"), []) is False
    assert _time.monotonic() - started < 1.0
    assert calls == [1]  # later calls skip the open during the cooldown
    assert ledger.write_failures == 5


def test_read_paths_never_create_an_absent_ledger(tmp_path):
    """With Jev off there is no ledger; dashboards, metrics and exports must not create one."""
    from memorymaster.decisions.export import export_jsonl
    from memorymaster.decisions.metrics import compute_metrics

    path = tmp_path / "absent" / "decisions.db"
    ledger = DecisionLedger(path)
    assert ledger.query("SELECT * FROM decisions") == []
    assert ledger.get_watermark("lifecycle_events") is None
    report = compute_metrics(ledger)
    assert isinstance(report, dict)
    assert export_jsonl(ledger, tmp_path / "out" / "rows.jsonl") == 0
    assert not path.exists() and not path.parent.exists()
    assert ledger.write_decision(DecisionRecord(decision_id="first")) is True  # writes still create it
    assert [r["decision_id"] for r in ledger.query("SELECT decision_id FROM decisions")] == ["first"]


def test_query_distinguishes_a_failed_read_from_an_empty_result(ledger, tmp_path):
    assert DecisionLedger(tmp_path / "absent.db").query("SELECT * FROM decisions") == []  # absent: empty
    assert ledger.write_decision(_decision("d1"), []) is True
    assert ledger.query("SELECT * FROM decisions WHERE decision_id = ?", ("nope",)) == []  # empty
    with pytest.raises(ld.LedgerReadError):
        ledger.query("SELECT * FROM no_such_table")  # a read that failed is not "no rows"
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"not a sqlite database at all" * 64)
    with pytest.raises(ld.LedgerReadError):
        DecisionLedger(broken).query("SELECT * FROM decisions")


def test_metrics_and_export_do_not_report_an_unreadable_ledger_as_empty(tmp_path):
    from memorymaster.decisions.export import export_jsonl
    from memorymaster.decisions.metrics import compute_metrics

    broken = tmp_path / "broken.db"
    broken.write_bytes(b"not a sqlite database at all" * 64)
    with pytest.raises(ld.LedgerReadError):
        compute_metrics(DecisionLedger(broken))
    with pytest.raises(ld.LedgerReadError):
        export_jsonl(DecisionLedger(broken), tmp_path / "out.jsonl")


# ------------------------------------------------- bounded (hook) connections ---

def _hold_write_lock(path) -> sqlite3.Connection:
    """Another process's writer: a BEGIN IMMEDIATE held until the test releases it."""
    holder = sqlite3.connect(str(path), timeout=5, isolation_level=None, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")
    return holder


def test_bounded_writes_wait_at_most_the_bound_for_another_writer(tmp_path):
    import time

    path = tmp_path / "decisions.db"
    assert DecisionLedger(path).write_decision(_decision("seed")) is True
    ledger = DecisionLedger(path)  # a fresh instance, as in a new hook process
    before = ld.ledger_write_failures()
    holder = _hold_write_lock(path)
    try:
        started = time.perf_counter()
        with ledger.bounded(200):
            written = ledger.write_decision(_decision("blocked"))
        elapsed = time.perf_counter() - started
    finally:
        holder.rollback()
        holder.close()
    assert written is False and elapsed < 0.8
    assert ld.ledger_write_failures() > before
    assert ledger.write_decision(_decision("after")) is True  # a lock is not a cooldown


def test_reads_never_need_the_write_lock_once_the_schema_is_current(tmp_path):
    import time

    path = tmp_path / "decisions.db"
    seeded = DecisionLedger(path)
    assert seeded.write_decision(_decision("seed", cost_usd=0.25)) is True
    assert seeded.set_watermark("breaker:recall", "2030-01-01T00:00:00+00:00") is True
    ledger = DecisionLedger(path)
    holder = _hold_write_lock(path)
    try:
        started = time.perf_counter()
        with ledger.bounded(200):
            spend = ledger.spend_since("2000-01-01")
            mark = ledger.get_watermark("breaker:recall")
        elapsed = time.perf_counter() - started
    finally:
        holder.rollback()
        holder.close()
    assert spend == pytest.approx(0.25) and mark == "2030-01-01T00:00:00+00:00"
    assert elapsed < 0.5


def test_bounded_scope_is_per_thread_and_restored(tmp_path):
    import threading

    ledger = DecisionLedger(tmp_path / "decisions.db")
    seen: list = []
    with ledger.bounded(120):
        seen.append(ledger._effective_busy_ms())
        other = threading.Thread(target=lambda: seen.append(ledger._effective_busy_ms()))
        other.start()
        other.join()
        with ledger.bounded(30):
            seen.append(ledger._effective_busy_ms())
        seen.append(ledger._effective_busy_ms())
    seen.append(ledger._effective_busy_ms())
    assert seen == [120, ld.BUSY_TIMEOUT_MS, 30, 120, ld.BUSY_TIMEOUT_MS]


def test_a_failed_strict_watermark_read_is_not_an_absent_watermark(tmp_path, monkeypatch):
    path = tmp_path / "decisions.db"
    ledger = DecisionLedger(path)
    assert ledger.get_watermark("breaker:recall", strict=True) is None  # no ledger yet: absent, not failed
    assert ledger.set_watermark("breaker:recall", "2030-01-01T00:00:00+00:00") is True
    assert ledger.get_watermark("breaker:recall", strict=True) == "2030-01-01T00:00:00+00:00"
    before = ld.ledger_write_failures()
    monkeypatch.setattr(ld, "_open_bounded", lambda _path: (_ for _ in ()).throw(
        sqlite3.OperationalError("database is locked")))
    with ledger.bounded(30):
        assert ledger.get_watermark("breaker:recall") is None  # lenient callers keep their old contract
        with pytest.raises(ld.LedgerReadError):
            ledger.get_watermark("breaker:recall", strict=True)
    assert ld.ledger_write_failures() > before  # a read that forces a fallback is counted


_CONTENDED_WRITER = textwrap.dedent(
    """
    import sys, time
    from memorymaster.decisions.ledger import DecisionLedger, DecisionRecord
    path, worker, seconds = sys.argv[1], sys.argv[2], float(sys.argv[3])
    ledger = DecisionLedger(path)
    end, n = time.monotonic() + seconds, 0
    while time.monotonic() < end:
        with ledger.bounded(250):
            ledger.write_decision(DecisionRecord(decision_id=f"w{worker}-{n}", surface="recall"))
        n += 1
    print(n)
    """
)


def test_bounded_reads_survive_concurrent_hook_writers(tmp_path):
    """Hook processes open, write and close the ledger all the time; a bounded read in
    between (WAL recovery, header reads) must be retried within the bound, never
    reported as a missing watermark (verifier B1: 7 failed watermark reads in a race)."""
    import time

    path = tmp_path / "decisions.db"
    seeded = DecisionLedger(path)
    assert seeded.set_watermark("breaker:recall", "2030-01-01T00:00:00+00:00") is True
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MEMORYMASTER_", "TYPESAFE_"))}
    env["PYTHONPATH"] = str(REPO)
    writers = [
        subprocess.Popen([sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {str(REPO)!r})\n"
                          + _CONTENDED_WRITER, str(path), str(w), "3.0"],
                         cwd=str(tmp_path), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for w in range(6)
    ]
    # The 250 ms hook bound is a wall-clock limit: under MEMORYMASTER_SKIP_PERF (shared CI
    # runners, one miss in 81 on Windows 2026-09-24) the retry is exercised with a wider bound.
    timing = not os.environ.get("MEMORYMASTER_SKIP_PERF")
    bound_ms, min_reads = (250, 50) if timing else (2000, 20)
    misses, reads = 0, 0
    try:
        time.sleep(0.4)  # let the writers start contending
        end = time.monotonic() + 2.0
        while time.monotonic() < end:
            reader = DecisionLedger(path)  # a fresh hook process each time
            with reader.bounded(bound_ms):
                try:
                    value = reader.get_watermark("breaker:recall", strict=True)
                except ld.LedgerReadError:
                    value = None
            reads += 1
            misses += value != "2030-01-01T00:00:00+00:00"
    finally:
        outputs = [p.communicate(timeout=60) for p in writers]
    assert all(p.returncode == 0 for p in writers), [err for _, err in outputs]
    assert reads > min_reads and misses == 0, (misses, reads)


def test_registered_questions_are_read_first_and_need_no_write_lock(tmp_path):
    """Every hook process registers its questions: once they exist, that is a read, not a
    write transaction (no contention with other writers)."""
    import time

    path = tmp_path / "decisions.db"
    known, new = get_question("recall.usable_evidence"), get_question("recall.relevant")
    assert DecisionLedger(path).register_questions([known]) is True
    ledger = DecisionLedger(path)  # a fresh instance, as in a new hook process
    holder = _hold_write_lock(path)
    try:
        started = time.perf_counter()
        with ledger.bounded(100):
            already = ledger.register_questions([known])
            missing = ledger.register_questions([known, new])
        elapsed = time.perf_counter() - started
    finally:
        holder.rollback()
        holder.close()
    assert already is True  # nothing to write
    assert missing is False  # a missing version still needs the (locked) write
    assert elapsed < 0.6, elapsed
    assert ledger.register_questions([known, new]) is True
    assert {row["question_id"] for row in ledger.query("SELECT question_id FROM question_versions")} == {
        known.id, new.id}

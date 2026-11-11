"""Contracts of the daily health probe (T-2121): it must fail on a broken user outcome, never write the DB,
and alert on a state change, not on a schedule. Every fixture is a temp DB or dir; the live DB, hooks and
services are never touched."""

from __future__ import annotations

import contextlib
import hashlib
import http.server
import importlib.util
import json
import re
import shutil
import sqlite3
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from memorymaster.core.service import MemoryService
from memorymaster.recall.verbatim_store import ensure_verbatim_schema

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("memory_health_probe", ROOT / "scripts" / "memory_health_probe.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
CFG = {**probe.CONFIG, "services": {}, "dense_url": "http://127.0.0.1:9/health", "service_timeout_s": 1.0}  # port 9: nothing listens


@contextlib.contextmanager
def edit(target, **kwargs):
    """Open, commit and CLOSE (sqlite3's own `with` leaves the file locked on Windows)."""
    conn = sqlite3.connect(target, **kwargs)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def iso(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat(timespec="seconds")


def add_claim(conn, agent, hours_ago, *, status="confirmed", tier="working", human_id=None, text="claim text"):
    cur = conn.execute(
        "INSERT INTO claims (text, scope, status, created_at, updated_at, source_agent, tier, human_id) "
        "VALUES (?, 'project:fixture', ?, ?, ?, ?, ?, ?)",
        (text, status, iso(hours_ago), iso(hours_ago), agent, tier, human_id))
    return cur.lastrowid


class World:
    """A temp authoritative DB plus the two side stores, all fresh unless a test ages one."""

    def __init__(self, tmp: Path, schema_template: Path) -> None:
        self.tmp = tmp
        self.db, self.transcripts, self.capture = tmp / "mm.db", tmp / "transcripts.db", tmp / "capture.db"
        shutil.copy(schema_template, self.db)
        with self.rw() as conn:
            conn.execute("INSERT INTO verbatim_memories (session_id, role, content, timestamp) VALUES ('s','user','hi',?)", (iso(1),))
            for agent in ("claude-session", "llm-stop-hook", "dream-worker"):
                add_claim(conn, agent, 1)
            cid = add_claim(conn, "claude-session", 2)
            self.claim_id = cid
        with edit(self.transcripts) as conn:
            conn.execute("CREATE TABLE files (rel TEXT PRIMARY KEY, mtime_ns INTEGER NOT NULL)")
            conn.execute("INSERT INTO files VALUES ('a', ?)", (int((NOW - timedelta(hours=1)).timestamp() * 1e9),))
        with edit(self.capture) as conn:
            conn.execute("CREATE TABLE dream_captures (id INTEGER PRIMARY KEY, state TEXT, captured_at TEXT, "
                         "last_activity_at TEXT, turn_count INTEGER, updated_at TEXT)")
            conn.execute("INSERT INTO dream_captures (state, captured_at, last_activity_at, turn_count) "
                         "VALUES ('captured', ?, ?, 3)", (iso(2), iso(2)))
            conn.execute("CREATE TABLE dream_runs (run_id TEXT PRIMARY KEY, status TEXT NOT NULL, dry_run INTEGER NOT NULL, "
                         "extractor_model TEXT, consolidator_model TEXT, started_at TEXT NOT NULL, heartbeat_at TEXT, "
                         "completed_at TEXT, summary_json TEXT)")

    @contextlib.contextmanager
    def rw(self):
        conn = sqlite3.connect(self.db)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def ingest_checks(self) -> dict[str, dict]:
        return {c["name"]: c for c in probe.check_ingest(self.db, self.transcripts, self.capture, NOW, CFG)}


@pytest.fixture(scope="module")
def schema_template(tmp_path_factory):
    """init_db migrates a full schema (about a minute on a loaded host): do it once, copy per test."""
    root = tmp_path_factory.mktemp("schema")
    MemoryService(db_target=str(root / "t.db"), workspace_root=root).init_db()
    ensure_verbatim_schema(str(root / "t.db"))  # the table is created out-of-band, not by init_db
    with edit(root / "t.db") as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return root / "t.db"


@pytest.fixture
def world(tmp_path, schema_template):
    return World(tmp_path, schema_template)


# --------------------------------------------------------------------------- 1. ingest freshness

def test_fresh_stores_pass_every_ingest_check(world):
    checks = world.ingest_checks()
    assert {c["status"] for c in checks.values()} == {"PASS"}, checks
    assert {"ingest.verbatim", "ingest.transcripts", "ingest.claims.session", "ingest.claims.hook",
            "ingest.claims.dream-worker", "ingest.capture_queue"} <= set(checks)


def test_a_stale_verbatim_store_fails(world):
    with world.rw() as conn:
        conn.execute("UPDATE verbatim_memories SET timestamp=?", (iso(24 * 70),))
    assert world.ingest_checks()["ingest.verbatim"]["status"] == "FAIL"


def test_a_stale_transcript_index_fails(world):
    with edit(world.transcripts) as conn:
        conn.execute("UPDATE files SET mtime_ns=?", (int((NOW - timedelta(hours=500)).timestamp() * 1e9),))
    assert world.ingest_checks()["ingest.transcripts"]["status"] == "FAIL"


@pytest.mark.parametrize("agent,check", [
    ("claude-session", "ingest.claims.session"), ("llm-stop-hook", "ingest.claims.hook"), ("dream-worker", "ingest.claims.dream-worker")])
def test_a_silent_claim_source_family_fails(world, agent, check):
    with world.rw() as conn:
        conn.execute("UPDATE claims SET created_at=? WHERE source_agent=?", (iso(24 * 30), agent))
    checks = world.ingest_checks()
    assert checks[check]["status"] == "FAIL"
    assert [n for n, c in checks.items() if n.startswith("ingest.claims") and c["status"] == "FAIL"] == [check]


def test_capture_queue_fails_on_backlog_size_and_on_age(world):
    with edit(world.capture) as conn:
        conn.execute("UPDATE dream_captures SET last_activity_at=?", (iso(24 * 30),))
    assert world.ingest_checks()["ingest.capture_queue"]["status"] == "FAIL"  # one very old capture
    with edit(world.capture) as conn:
        conn.execute("UPDATE dream_captures SET last_activity_at=?", (iso(1),))
        conn.executemany("INSERT INTO dream_captures (state, captured_at, last_activity_at, turn_count) "
                         "VALUES ('captured', ?, ?, 2)", [(iso(1), iso(1))] * 1100)
    assert world.ingest_checks()["ingest.capture_queue"]["status"] == "FAIL"  # many fresh ones
    with edit(world.capture) as conn:
        conn.execute("UPDATE dream_captures SET state='applied'")
    assert world.ingest_checks()["ingest.capture_queue"]["status"] == "PASS"


def test_capture_queue_counts_only_what_the_worker_can_still_pick_up(world):
    """ledger.eligible() needs turn_count >= 2 and state captured/retryable (operational_review counts the same):
    single-turn captures are never scheduled and never retired, so they must not trip the alarm (T-2123)."""
    with edit(world.capture) as conn:
        conn.executemany("INSERT INTO dream_captures (state, captured_at, last_activity_at, turn_count) "
                         "VALUES ('captured', ?, ?, 1)", [(iso(24 * 100), iso(24 * 100))] * 1500)
    check = world.ingest_checks()["ingest.capture_queue"]
    assert check["status"] == "PASS" and check["data"]["waiting"] == 1, check
    with edit(world.capture) as conn:  # a retryable multi-turn capture still waits; its age is the last write
        conn.execute("INSERT INTO dream_captures (state, captured_at, last_activity_at, turn_count) "
                     "VALUES ('retryable', ?, ?, 4)", (iso(24 * 100), iso(24 * 9)))
    check = world.ingest_checks()["ingest.capture_queue"]
    assert check["status"] == "FAIL" and check["data"]["waiting"] == 2 and check["data"]["oldest_age_h"] == 216, check


def test_one_unreadable_store_does_not_hide_the_others(world):
    with edit(world.transcripts) as conn:
        conn.execute("DROP TABLE files")  # the file opens; only the transcripts read raises
    checks = world.ingest_checks()
    assert checks["ingest.transcripts"]["status"] == "FAIL" and "could not run" in checks["ingest.transcripts"]["detail"]
    for healthy in ("ingest.verbatim", "ingest.claims.session", "ingest.claims.hook", "ingest.claims.dream-worker",
                    "ingest.capture_queue"):
        assert checks[healthy]["status"] == "PASS", healthy


def test_a_missing_store_is_a_fail_not_a_crash(world):
    world.transcripts.unlink()
    checks = world.ingest_checks()
    assert checks["ingest.transcripts"]["status"] == "FAIL" and "could not run" in checks["ingest.transcripts"]["detail"]


# --------------------------------------------------------------------------- 2. canary recall

def hit(human_id, text="", **kw):
    return probe.Hit(kw.pop("claim_id", 1), human_id, text, **kw)


def test_canary_hit_matches_id_ignoring_suffix_or_substring_inside_top_five_only():
    canary = {"expect_human_ids": ["mm-a1a0"], "expect_substrings": ["Needle"]}
    filler = [hit(f"mm-{i}") for i in range(5)]
    assert probe.canary_hit(canary, [hit("mm-a1a0~2")])
    assert probe.canary_hit(canary, [hit("mm-x", "some needle here")])
    assert not probe.canary_hit(canary, [hit("mm-zzzz", "unrelated")])
    assert not probe.canary_hit(canary, filler + [hit("mm-a1a0")]), "rank 6 is not hit@5"


def test_recall_paths_fail_under_the_hit_rate_threshold_and_report_each_path():
    canaries = [{"id": f"c{i}", "query": "q", "expect_human_ids": [f"mm-{i}"]} for i in range(4)]
    good = lambda: (lambda c: [hit(c["expect_human_ids"][0])])  # noqa: E731
    bad = lambda: (lambda c: [hit("mm-off-topic")])  # noqa: E731
    checks, _ = probe.check_canaries(canaries, {"hook": good, "mcp": bad}, CFG)
    by = {c["name"]: c for c in checks}
    assert by["recall.hook"]["status"] == "PASS" and by["recall.mcp"]["status"] == "FAIL"
    assert by["recall.mcp"]["data"]["missed"] == ["c0", "c1", "c2", "c3"]


def test_erroring_queries_and_a_path_that_cannot_start_count_as_failures():
    canaries = [{"id": "c", "query": "q", "expect_human_ids": ["mm-1"]}]

    def explode(_):
        raise RuntimeError("boom")

    def cannot_start():
        raise ImportError("no module")

    checks, _ = probe.check_canaries(canaries, {"hook": lambda: explode, "mcp": cannot_start}, CFG)
    by = {c["name"]: c for c in checks}
    assert by["recall.hook"]["status"] == "FAIL" and by["recall.hook"]["data"]["errors"] == 1
    assert by["recall.mcp"]["status"] == "FAIL" and "could not start" in by["recall.mcp"]["detail"]


# --------------------------------------------------------------------------- 3. ranking sanity

def test_ranking_fails_when_core_claims_sit_at_the_freshness_floor():
    floor = [hit("a", claim_id=i, tier="core", status="confirmed", freshness=1e-10) for i in range(5)]
    assert probe.check_ranking(floor, CFG)[0]["status"] == "FAIL"
    healthy = [hit("a", claim_id=i, tier="core", status="confirmed", freshness=0.5) for i in range(5)]
    assert probe.check_ranking(healthy, CFG)[0]["status"] == "PASS"


def test_ranking_ignores_non_core_and_non_confirmed_rows_and_fails_with_no_core_rows():
    noise = [hit("a", claim_id=1, tier="working", status="confirmed", freshness=1e-12),
             hit("b", claim_id=2, tier="core", status="stale", freshness=1e-12),
             hit("c", claim_id=3, tier="core", status="confirmed", freshness=0.9)]
    assert probe.check_ranking(noise, CFG)[0]["status"] == "PASS"
    only_noise = probe.check_ranking(noise[:2], CFG)[0]
    assert only_noise["status"] == "FAIL" and "no core confirmed" in only_noise["detail"]


# --------------------------------------------------------------------------- 4. retention

def test_retention_fails_when_the_oldest_verbatim_row_is_past_the_archive_window(world):
    assert probe.check_retention(world.db, NOW)[0]["status"] == "PASS"
    with world.rw() as conn:
        # retention_due reads the lowest id, so the old row only counts when it is the head of the table
        conn.execute("INSERT INTO verbatim_memories (id, session_id, role, content, timestamp) VALUES (0, 'old','user','x',?)", (iso(24 * 200),))
    result = probe.check_retention(world.db, NOW)[0]
    assert result["status"] == "FAIL" and result["data"]["oldest_age_days"] >= 199


def write_marker(tmp_path, **fields):
    path = tmp_path / "verbatim-health.json"
    path.write_text(json.dumps(fields), encoding="utf-8")
    return path


def test_verbatim_marker_passes_when_absent_or_healthy(tmp_path):
    assert probe.check_verbatim_marker(tmp_path / "none.json", NOW, CFG)[0]["status"] == "PASS"
    healthy = write_marker(tmp_path, retention_due=False, last_store_at=iso(1), store_skipped_count=4, last_skip_at=iso(24 * 9))
    assert probe.check_verbatim_marker(healthy, NOW, CFG)[0]["status"] == "PASS", "an old skip is history, not an alarm"


@pytest.mark.parametrize("fields,reason", [
    ({"retention_due": True}, "retention_due"),
    ({"retention_check_error": "OperationalError"}, "retention check failed"),
    ({"last_skip_at": "RECENT", "last_skip_reason": "gate closed", "store_skipped_count": 1}, "store skipped"),
])
def test_verbatim_marker_fails_on_overdue_retention_a_failed_check_or_a_recent_skip(tmp_path, fields, reason):
    fields = {k: (iso(2) if v == "RECENT" else v) for k, v in fields.items()}
    result = probe.check_verbatim_marker(write_marker(tmp_path, **fields), NOW, CFG)[0]
    assert result["status"] == "FAIL" and reason in result["detail"]


def test_a_corrupt_marker_is_a_fail_not_a_crash(tmp_path):
    path = tmp_path / "verbatim-health.json"
    path.write_text("{not json", encoding="utf-8")
    checks = probe._guard("verbatim.marker", probe.check_verbatim_marker, path, NOW, CFG)
    assert checks[0]["status"] == "FAIL" and "could not run" in checks[0]["detail"]


# --------------------------------------------------------------------------- 5. services

@pytest.fixture
def server():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            code, body = (200, b'{"status": "ok"}') if self.path == "/ok" else (200, b'{"ok": false}') if self.path == "/notok" else (500, b"{}")
            self.send_response(code)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def test_services_pass_only_on_a_healthy_answer(server):
    dead = "http://127.0.0.1:9/ok"
    cfg = {**CFG, "service_timeout_s": 1.0,
           "services": {"up": f"{server}/ok", "flag_off": f"{server}/notok", "error": f"{server}/err", "down": dead}}
    by = {c["name"]: c["status"] for c in probe.check_services(cfg)}
    assert by == {"service.up": "PASS", "service.flag_off": "FAIL", "service.error": "FAIL", "service.down": "FAIL"}


# --------------------------------------------------------------------------- dense recall service

@pytest.fixture
def dense(world):
    """A fake dense /health; `state` is edited per test. The world has 4 confirmed claims."""
    state = {"body": {"ok": True, "indexed": 4, "last_sync": NOW.timestamp() - 30, "sync_errors": 0}, "code": 200}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(state["code"])
            self.end_headers()
            self.wfile.write(json.dumps(state["body"]).encode())

        def log_message(self, *a):
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    cfg = {**CFG, "dense_url": f"http://127.0.0.1:{httpd.server_port}/health"}
    yield state, lambda: {c["name"]: c for c in probe.check_dense(world.db, NOW, cfg)}
    httpd.shutdown()


def test_a_healthy_dense_service_passes_all_three_checks(dense):
    _state, run = dense
    checks = run()
    assert {n: c["status"] for n, c in checks.items()} == {"dense.indexed": "PASS", "dense.sync_fresh": "PASS", "dense.sync_errors": "PASS"}


@pytest.mark.parametrize("change,failing", [
    ({"indexed": 1}, "dense.indexed"),  # 1 of 4 confirmed claims
    ({"last_sync": NOW.timestamp() - 3600}, "dense.sync_fresh"),
    ({"last_sync": None}, "dense.sync_fresh"),
    ({"sync_errors": 3}, "dense.sync_errors"),
])
def test_each_dense_symptom_fails_only_its_own_check(dense, change, failing):
    state, run = dense
    state["body"] = {**state["body"], **change}
    assert [n for n, c in run().items() if c["status"] == "FAIL"] == [failing]


def test_a_dense_service_that_does_not_answer_fails_with_a_clear_reason(dense, world):
    state, run = dense
    state["body"] = {"ok": False}
    flagged = run()
    assert list(flagged) == ["dense.service"] and flagged["dense.service"]["status"] == "FAIL"
    down = probe.check_dense(world.db, NOW, CFG)  # CFG points at a port nobody listens on
    assert down[0]["name"] == "dense.service" and down[0]["status"] == "FAIL" and "did not answer" in down[0]["detail"]


# --------------------------------------------------------------------------- read-only guarantee

def test_the_probe_never_writes_the_database(world, monkeypatch):
    with world.rw() as conn:
        # global scope is inside both recall paths' allowlists, so the canaries really return (and would record) this claim
        conn.execute("UPDATE claims SET tier='core', human_id='mm-fx01', scope='global', "
                     "text='fixture lookup canary claim about spool isolation' WHERE id=?", (world.claim_id,))
    canaries = world.tmp / "canaries.json"
    canaries.write_text(json.dumps({"canaries": [
        {"id": "x", "query": "fixture lookup canary claim about spool isolation", "expect_human_ids": ["mm-fx01"]}]}), encoding="utf-8")

    def digest():
        with edit(f"file:{world.db.as_posix()}?mode=ro", uri=True) as conn:
            return [conn.execute("SELECT COUNT(*), COALESCE(SUM(access_count),0) FROM claims").fetchone(),
                    conn.execute("SELECT COUNT(*) FROM verbatim_memories").fetchone()]

    before_file = hashlib.sha256(world.db.read_bytes()).hexdigest()
    before_rows = digest()
    opened: list[str] = []
    real_connect = sqlite3.connect

    def spy(target, *args, **kwargs):
        opened.append(str(target))
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", spy)
    spool = world.tmp / "must-stay-empty"
    monkeypatch.setenv("MEMORYMASTER_SPOOL_DIR", str(spool))
    report = probe.run_probe(world.db, workspace=world.tmp, canaries_path=canaries, transcripts_db=world.transcripts,
                             capture_db=world.capture, now=NOW, cfg=CFG)
    monkeypatch.setattr(sqlite3, "connect", real_connect)

    to_db = [t for t in opened if world.db.name in t]
    assert to_db and all("mode=ro" in t for t in to_db), to_db
    assert hashlib.sha256(world.db.read_bytes()).hexdigest() == before_file
    assert digest() == before_rows, "no access_count bump: a canary must not reinforce itself"
    assert not spool.exists(), "recall side files are redirected, then discarded"
    by = {c["name"]: c for c in report["checks"]}
    # both paths really returned the claim: the access-recording branch ran, so the guarantees above are not vacuous
    assert by["recall.mcp"]["data"]["hits"] == 1 and by["recall.hook"]["data"]["hits"] == 1, by


def test_the_env_redirect_is_restored_after_the_run(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_SPOOL_DIR", "original")
    monkeypatch.delenv("MEMORYMASTER_RECALL_DENSE_LOG", raising=False)
    with probe.isolated_recall_env(tmp_path):
        assert probe.os.environ["MEMORYMASTER_SPOOL_DIR"] != "original"
    assert probe.os.environ["MEMORYMASTER_SPOOL_DIR"] == "original"
    assert "MEMORYMASTER_RECALL_DENSE_LOG" not in probe.os.environ


# --------------------------------------------------------------------------- report + alert

def failing_report_plain(*names, day="2026-11-03"):
    return {"generated_at": f"{day}T09:15:00+00:00", "failures": list(names), "status": "FAIL" if names else "PASS",
            "checks": [{"name": n, "status": "FAIL", "detail": f"{n} is broken"} for n in names] + [{"name": "ok", "status": "PASS", "detail": ""}]}


@pytest.fixture
def orchestrators(tmp_path):
    path = tmp_path / "orchestrators.json"
    path.write_text(json.dumps({"fleet": {"run_id": "run_fixture"}}), encoding="utf-8")
    return path


def failing_report(*names, day="2026-11-03", **data):
    """A report whose failing checks carry ``data`` (e.g. waiting=...), the numbers "materially worse" compares."""
    report = failing_report_plain(*names, day=day)
    for check in report["checks"]:
        if check["status"] == probe.FAIL and data:
            check["data"] = dict(data)
    return report


def alerts(out, orchestrators, *reports):
    """Run each report through maybe_alert in order; returns the list of (sent, subject-or-None, body-or-None)."""
    sent: list[tuple] = []
    results = []
    for report in reports:
        before = len(sent)
        got = probe.maybe_alert(report, out, orchestrators, sender=lambda run, subject, body: sent.append((subject, body)))
        results.append((got["sent"], *(sent[-1] if len(sent) > before else (None, None))))
    return results


def test_alert_fires_on_a_new_red_check_not_on_the_clock(tmp_path, orchestrators):
    results = alerts(tmp_path, orchestrators,
                     failing_report("a", "b"),
                     failing_report("b", "a"),                    # same set, same day
                     failing_report("a", "b", day="2026-11-04"),  # next day, nothing changed
                     failing_report("a", "b", "c", day="2026-11-05"),  # c turned red
                     failing_report("a", "b", "c", day="2026-11-06"))
    assert [r[0] for r in results] == [True, False, False, True, False]
    assert results[0][1] == "[MM-SALUD] 2 fallas" and results[3][1] == "[MM-SALUD] 3 fallas"
    assert "c: fail" in results[3][2] and "[new]" in results[3][2]


def test_a_check_that_stays_red_is_reminded_once_per_reminder_window(tmp_path, orchestrators):
    days = ["2026-11-03", "2026-11-09", "2026-11-10", "2026-11-16", "2026-11-17"]  # window is 7 days
    results = alerts(tmp_path, orchestrators, *[failing_report("a", day=d) for d in days])
    assert [r[0] for r in results] == [True, False, True, False, True]
    assert "reminder, still failing since 2026-11-03" in results[2][2]


def test_a_red_check_that_gets_materially_worse_alerts_again(tmp_path, orchestrators):
    results = alerts(tmp_path, orchestrators,
                     failing_report("ingest.capture_queue", waiting=1000),
                     failing_report("ingest.capture_queue", waiting=1200, day="2026-11-04"),  # +20 %: noise
                     failing_report("ingest.capture_queue", waiting=1500, day="2026-11-05"),  # x1.5 of the last alert
                     failing_report("ingest.capture_queue", waiting=900, day="2026-11-06"))   # better: quiet
    assert [r[0] for r in results] == [True, False, True, False]
    assert "worse than at the last alert" in results[2][2]


def test_recovery_is_announced_once_and_a_later_relapse_alerts_again(tmp_path, orchestrators):
    results = alerts(tmp_path, orchestrators,
                     failing_report("a", "b"),
                     failing_report("a", day="2026-11-04"),   # b recovered, a still red
                     failing_report(day="2026-11-05"),        # everything recovered
                     failing_report(day="2026-11-06"),        # still green: silence
                     failing_report("a", day="2026-11-07"))   # relapse
    assert [r[0] for r in results] == [True, True, True, False, True]
    assert "b: pass — back to green" in results[1][2] and "a: fail" in results[1][2]
    assert results[2][1] == "[MM-SALUD] recuperado" and "a: pass — back to green" in results[2][2]
    assert "[new]" in results[4][2]


def test_a_recovery_notice_keeps_the_voz_line_lint_clean_and_digit_free(tmp_path, orchestrators):
    body = probe.alert_body(failing_report(), recovered=("a",))
    voz = [line for line in body.splitlines() if line.startswith("voz:")][0]
    assert not re.search(r"\d", voz) and voz.endswith(".")


def test_age_is_never_a_severity_so_a_red_age_check_is_not_re_announced_as_time_passes(tmp_path, orchestrators):
    results = alerts(tmp_path, orchestrators,
                     failing_report("steward.recency", age_h=12.5, day="2026-11-03"),
                     failing_report("steward.recency", age_h=36.5, day="2026-11-04"),
                     failing_report("steward.recency", age_h=60.5, day="2026-11-05"))
    assert [r[0] for r in results] == [True, False, False]


def test_a_non_numeric_severity_in_the_state_file_is_ignored_and_the_state_is_rewritten(tmp_path, orchestrators):
    (tmp_path / "alert-state.json").write_text(json.dumps(
        {"utc_date": "2026-11-03", "failures": ["a"], "checks": {"a": {"on": "2026-11-03", "severity": "high"}}}), encoding="utf-8")
    results = alerts(tmp_path, orchestrators, failing_report("a", waiting=5000, day="2026-11-04"),
                     failing_report("a", waiting=5000, day="2026-11-10"))
    assert [r[0] for r in results] == [False, True], "no crash; unknown baseline means only the reminder fires"
    state = json.loads((tmp_path / "alert-state.json").read_text(encoding="utf-8"))
    assert state["checks"]["a"]["severity"] == 5000.0


def test_the_old_alert_state_file_is_still_read(tmp_path, orchestrators):
    (tmp_path / "alert-state.json").write_text(json.dumps({"utc_date": "2026-11-03", "failures": ["a", "b"]}), encoding="utf-8")
    results = alerts(tmp_path, orchestrators,
                     failing_report("a", "b", day="2026-11-04"),  # the old file already covers both
                     failing_report("a", "b", "c", day="2026-11-05"))
    assert [r[0] for r in results] == [False, True]
    state = json.loads((tmp_path / "alert-state.json").read_text(encoding="utf-8"))
    assert state["failures"] == ["a", "b", "c"] and set(state["checks"]) == {"a", "b", "c"}  # new shape, old keys kept


def test_a_corrupt_alert_state_is_treated_as_empty(tmp_path, orchestrators):
    (tmp_path / "alert-state.json").write_text("[1, 2", encoding="utf-8")
    assert alerts(tmp_path, orchestrators, failing_report("a"))[0][0] is True


def test_a_failed_send_is_retried_next_run_and_never_raises(tmp_path, orchestrators):
    def broken(*_):
        raise RuntimeError("orca down")

    out = tmp_path
    result = probe.maybe_alert(failing_report("a"), out, orchestrators, sender=broken)
    assert not result["sent"] and "send failed" in result["reason"]
    assert not (out / "alert-state.json").exists()
    assert probe.maybe_alert(failing_report("a"), out, orchestrators, sender=lambda *_: None)["sent"]


class FakeOrca:
    """Stands in for subprocess.run: records argv and env, answers with a canned result."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.calls: list[tuple[list, dict]] = []
        self.result = subprocess.CompletedProcess([], returncode, stdout, stderr)

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs.get("env")))  # None = the child would inherit os.environ
        return self.result


def test_the_alert_is_signed_with_a_label_never_a_terminal_handle(monkeypatch):
    monkeypatch.setenv("ORCA_TERMINAL_HANDLE", "term_lane_handle")
    monkeypatch.delenv("MEMORYMASTER_HEALTH_PROBE_SENDER", raising=False)
    fake = FakeOrca()
    monkeypatch.setattr(probe.subprocess, "run", fake)
    probe.send_orca("run_fixture", "[MM-SALUD] 2 fallas", "body")
    argv, env = fake.calls[0]
    assert argv[argv.index("--from") + 1] == "memorymaster-health-probe"
    assert argv[argv.index("--type") + 1] == "status" and argv[argv.index("--to") + 1] == "run:run_fixture"
    assert env is not None, "env must be passed explicitly: a missing env means the child inherits ORCA_TERMINAL_HANDLE"
    assert "term_lane_handle" not in argv and "ORCA_TERMINAL_HANDLE" not in env


def test_the_sender_label_can_be_overridden_by_env(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_HEALTH_PROBE_SENDER", "  probe-staging ")
    fake = FakeOrca()
    monkeypatch.setattr(probe.subprocess, "run", fake)
    probe.send_orca("run_fixture", "s", "b")
    argv, _ = fake.calls[0]
    assert argv[argv.index("--from") + 1] == "probe-staging"


def test_a_refused_send_keeps_the_exit_code_non_zero_and_the_report_says_why(tmp_path, monkeypatch, orchestrators):
    report = {**failing_report("ingest.verbatim"), "env": {}}
    monkeypatch.setattr(probe, "run_probe", lambda *a, **k: dict(report))
    monkeypatch.setattr(probe.subprocess, "run", FakeOrca(returncode=1, stderr='{"code": "no_active_sender_terminal"}'))
    out = tmp_path / "health"
    args = ["--db", "x.db", "--out", str(out), "--orchestrators", str(orchestrators), "--now", "2026-11-03T09:15:00+00:00"]
    assert probe.main(args) == 1
    alert = json.loads((out / "latest.json").read_text(encoding="utf-8"))["alert"]
    assert alert["sent"] is False and "no_active_sender_terminal" in alert["reason"] and "orca exit 1" in alert["reason"]
    assert not (out / "alert-state.json").exists(), "a refused send is retried on the next run"


def test_a_refusal_reported_on_stdout_is_still_explained():
    fake = FakeOrca(returncode=2, stdout="sender_not_assignee")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(probe.subprocess, "run", fake)
        with pytest.raises(RuntimeError, match="sender_not_assignee"):
            probe.send_orca("run_fixture", "s", "b")


def test_alert_body_carries_criteria_and_a_digit_free_voz_line():
    body = probe.alert_body(failing_report("ingest.verbatim", "retention.due"))
    assert body.startswith("criteria:") and "ingest.verbatim: fail" in body and "files_changed:" in body
    voz = re.search(r"^voz: (.+)$", body, re.M).group(1)
    assert voz == probe.VOZ and not re.search(r"\d", voz) and len(voz.split()) >= 5


def test_the_voz_line_passes_the_wezbridge_lint():
    lint = next((p / "wezbridge" / "scripts" / "lint-voz-line.cjs" for p in ROOT.parents
                 if (p / "wezbridge" / "scripts" / "lint-voz-line.cjs").is_file()), ROOT / "missing")
    node = shutil.which("node")
    if not (lint.is_file() and node):
        pytest.skip("wezbridge lint-voz-line.cjs or node not available on this host")
    done = subprocess.run([node, str(lint), "--project", "memorymaster", "-"], input=probe.alert_body(failing_report("a")),
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr


def test_main_writes_dated_and_latest_reports_exits_nonzero_and_honours_no_alert(tmp_path, monkeypatch, orchestrators):
    report = {**failing_report("ingest.verbatim"), "env": {}}
    monkeypatch.setattr(probe, "run_probe", lambda *a, **k: dict(report))
    sends: list[tuple] = []
    monkeypatch.setattr(probe, "send_orca", lambda *a: sends.append(a))
    out = tmp_path / "health"
    args = ["--db", "x.db", "--out", str(out), "--orchestrators", str(orchestrators), "--now", "2026-11-03T09:15:00+00:00"]

    assert probe.main(args + ["--no-alert"]) == 1
    assert sends == []
    latest = json.loads((out / "latest.json").read_text(encoding="utf-8"))
    assert latest["alert"]["reason"] == "--no-alert" and (out / "2026-11-03.json").is_file()

    assert probe.main(args) == 1 and len(sends) == 1
    report["failures"], report["checks"], report["status"] = [], [], "PASS"
    assert probe.main(args) == 0


def test_main_on_a_fresh_out_dir_writes_the_report_before_alerting_and_survives_a_failed_alert(tmp_path, monkeypatch, orchestrators):
    report = {**failing_report("ingest.verbatim"), "env": {}}
    monkeypatch.setattr(probe, "run_probe", lambda *a, **k: dict(report))
    out = tmp_path / "does" / "not" / "exist"
    on_disk_at_send: list[bool] = []

    def exploding_sender(*_):
        on_disk_at_send.append((out / "latest.json").is_file())  # the report already exists when the alert goes out
        raise RuntimeError("orca down")

    monkeypatch.setattr(probe, "send_orca", exploding_sender)
    args = ["--db", "x.db", "--out", str(out), "--orchestrators", str(orchestrators), "--now", "2026-11-03T09:15:00+00:00"]
    assert probe.main(args) == 1, "a failed send never masks the exit code"
    assert on_disk_at_send == [True]
    assert "send failed" in json.loads((out / "latest.json").read_text(encoding="utf-8"))["alert"]["reason"]

    def crashing_alert(*_a, **_k):
        raise OSError("state file unwritable")

    monkeypatch.setattr(probe, "maybe_alert", crashing_alert)
    assert probe.main(args) == 1
    assert "alert error" in json.loads((out / "latest.json").read_text(encoding="utf-8"))["alert"]["reason"]


def test_report_never_names_the_machine_path(world):
    canaries = world.tmp / "c.json"
    canaries.write_text(json.dumps({"canaries": []}), encoding="utf-8")
    report = probe.run_probe(world.db, workspace=world.tmp, canaries_path=canaries, transcripts_db=world.transcripts,
                             capture_db=world.capture, now=NOW, cfg=CFG, recallers={})
    assert report["db"] == "mm.db" and str(world.tmp) not in json.dumps(report)


def test_shipped_canaries_are_well_formed():
    data = json.loads((ROOT / "scripts" / "health_canaries.json").read_text(encoding="utf-8"))["canaries"]
    assert len(data) >= 8
    for canary in data:
        assert canary["id"] and len(canary["query"].split()) >= 4, "short queries are skipped as trivial by the prompt hook"
        assert canary.get("expect_human_ids") or canary.get("expect_substrings")


def test_main_leaves_global_logging_as_it_found_it(tmp_path, monkeypatch, orchestrators):
    """Regression: main() once called logging.disable(INFO) for good, which broke every later caplog test in the session."""
    import logging

    monkeypatch.setattr(probe, "run_probe", lambda *a, **k: {**failing_report(), "env": {}})
    before = logging.root.manager.disable
    probe.main(["--out", str(tmp_path), "--orchestrators", str(orchestrators), "--no-alert"])
    assert logging.root.manager.disable == before
    with probe.quiet_info_logs():
        assert logging.root.manager.disable == logging.INFO
    assert logging.root.manager.disable == before


# --------------------------------------------------------------------------- hooks.template_drift

TEMPLATE_HOOK = 'import os\nROOT = "__MEMORYMASTER_PROJECT_ROOT__"\nDB = __MEMORYMASTER_DB_PATH_REPR__  # noqa\n\ndef run():\n    return os.path.join(ROOT, "x")\n'


@pytest.fixture
def hooks(tmp_path, monkeypatch):
    """A tmp template dir with one synthetic template and an empty install dir per CLI."""
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "memorymaster-x.py").write_text(TEMPLATE_HOOK, encoding="utf-8")
    monkeypatch.setattr(probe, "TEMPLATE_DIR", templates)
    claude = tmp_path / "claude-hooks"
    claude.mkdir()
    return {"claude": claude, "allow": tmp_path / "hook-drift-allowlist.txt"}


def drift(hooks):
    (check,) = probe.check_hook_drift({"claude": hooks["claude"]}, hooks["allow"])
    return check


def test_a_placeholder_only_difference_is_not_drift(hooks):
    installed = TEMPLATE_HOOK.replace("__MEMORYMASTER_PROJECT_ROOT__", "/opt/mm/x y").replace(
        "__MEMORYMASTER_DB_PATH_REPR__", repr("/data/mm.db"))
    (hooks["claude"] / "memorymaster-x.py").write_bytes(installed.replace("\n", "\r\n").encode())  # CRLF too
    check = drift(hooks)
    assert check["status"] == probe.PASS and check["data"]["matching"] == 1, check


def test_a_logic_difference_is_drift_and_is_named_with_its_line_delta(hooks):
    installed = TEMPLATE_HOOK.replace("__MEMORYMASTER_PROJECT_ROOT__", "/opt/mm").replace(
        "__MEMORYMASTER_DB_PATH_REPR__", "'/d.db'").replace("os.path.join(ROOT", "os.path.join(DB")
    (hooks["claude"] / "memorymaster-x.py").write_text(installed, encoding="utf-8")
    check = drift(hooks)
    assert check["status"] == probe.FAIL and "claude/memorymaster-x.py (+1/-1)" in check["detail"], check
    assert "/opt/mm" not in check["detail"], "no machine path in the report"


def test_an_allowlisted_hook_is_reported_as_customised_not_failed(hooks):
    (hooks["claude"] / "memorymaster-x.py").write_text("print('operator edit')\n", encoding="utf-8")
    assert drift(hooks)["status"] == probe.FAIL
    hooks["allow"].write_text("# deliberate\nmemorymaster-x.py  # trailing note\n", encoding="utf-8")
    check = drift(hooks)
    assert check["status"] == probe.PASS and "customised by the operator: claude/memorymaster-x.py" in check["detail"], check
    assert check["data"]["drifted"] == 0 and check["data"]["customised"] == 1


def test_an_allowlist_with_a_bom_keeps_its_first_entry_and_a_typo_is_reported(hooks):
    (hooks["claude"] / "memorymaster-x.py").write_text("print('operator edit')\n", encoding="utf-8")
    hooks["allow"].write_bytes(b"\xef\xbb\xbfmemorymaster-x.py\nmemorymaster-typo.py\n")
    assert probe.read_hook_allowlist(hooks["allow"]) == {"memorymaster-x.py", "memorymaster-typo.py"}
    check = drift(hooks)
    assert check["status"] == probe.PASS and check["data"]["customised"] == 1
    assert "unknown allowlist entry (matches no installed hook): memorymaster-typo.py" in check["detail"]
    assert "(+1/-" in check["detail"], "the diff size of a customised hook stays visible"


def test_a_missing_allowlist_is_empty_and_a_template_with_no_installed_copy_is_a_note(hooks):
    assert probe.read_hook_allowlist(hooks["allow"]) == set() and probe.read_hook_allowlist(None) == set()
    check = drift(hooks)  # nothing installed at all
    assert check["status"] == probe.PASS and check["data"]["not_installed"] == 1 and "note" in check["detail"]


def test_every_real_template_rendered_by_its_installer_matches(tmp_path, monkeypatch):
    """The comparison must understand what the real installers write: claude/codex hooks, the agy adapter, OpenCode."""
    from memorymaster.surfaces import setup_agy_hooks, setup_hooks

    templates = ROOT / "memorymaster" / "config_templates" / "hooks"
    monkeypatch.setattr(probe, "TEMPLATE_DIR", templates)
    claude, codex = tmp_path / "claude", tmp_path / "codex"
    claude.mkdir()
    codex.mkdir()
    for template in templates.glob("*.py"):
        rendered = setup_hooks.replace_placeholder(template.read_text(encoding="utf-8"), tmp_path / "proj root")
        for target in (claude, codex):
            (target / template.name).write_text(rendered, encoding="utf-8")
    setup_agy_hooks.install_agy_hooks(gemini_config_dir=tmp_path / "gemini", project_root=tmp_path, db_path=tmp_path / "m.db")
    for version in (1, 2):
        setup_hooks.install_opencode_hooks(tmp_path / "opencode", version=version, project_root=tmp_path, python_exe="py")
        dirs = {"claude": claude, "codex": codex, "agy": tmp_path / "gemini" / "config", "opencode": tmp_path / "opencode"}
        (check,) = probe.check_hook_drift(dirs, None)
        assert check["status"] == probe.PASS, (version, check["detail"])
        assert check["data"]["not_installed"] == 0 and check["data"]["matching"] == 2 * len(list(templates.glob("*.py"))) + 3


# --------------------------------------------------------------------------- dreaming.runs, steward.recency

def add_runs(world, statuses, *, hours_ago=1.0, dry_run=0):
    with edit(world.capture) as conn:
        for i, status in enumerate(statuses):
            conn.execute("INSERT INTO dream_runs VALUES (?,?,?,?,?,?,?,?,?)",
                         (f"r{conn.execute('SELECT COUNT(*) FROM dream_runs').fetchone()[0]}-{i}", status, dry_run, "m", "m",
                          iso(hours_ago + i / 100), iso(hours_ago), iso(hours_ago), "{}"))


def runs(world):
    (check,) = probe.check_dream_runs(world.capture, NOW, CFG)
    return check


def test_dreaming_runs_are_red_above_half_failed_and_green_at_or_below(world):
    add_runs(world, ["ok", "ok", "partial", "failed"])
    assert runs(world)["status"] == probe.PASS
    add_runs(world, ["failed"])  # 2 of 5
    assert runs(world)["status"] == probe.PASS
    add_runs(world, ["failed", "failed"])  # 4 of 7
    check = runs(world)
    assert check["status"] == probe.FAIL and check["data"] == {"failed": 4, "abandoned": 0, "total": 7, "failed_share": 0.57}, check


def test_dreaming_runs_ignore_running_dry_and_out_of_window_runs(world):
    add_runs(world, ["running"] * 5)
    add_runs(world, ["failed"] * 5, dry_run=1)
    add_runs(world, ["failed"] * 5, hours_ago=60)  # older than 48 h
    add_runs(world, ["ok"])
    check = runs(world)
    assert check["status"] == probe.PASS and check["data"]["total"] == 1, check


def test_abandoned_runs_count_as_not_ok_and_are_reported_apart_from_failed(world):
    """The scheduler's time limit kills Beat runs and leaves them 'abandoned': 34, 56, 65 a day while nothing drained."""
    add_runs(world, ["ok", "abandoned", "abandoned", "failed", "partial"])
    check = runs(world)
    assert check["status"] == probe.FAIL and check["data"]["failed"] == 1 and check["data"]["abandoned"] == 2, check
    assert "1 failed and 2 abandoned of 5" in check["detail"]


def test_under_the_minimum_sample_only_the_newest_run_is_judged_against_the_schedule(world):
    add_runs(world, ["failed"], hours_ago=0.5)  # a lone failure inside the schedule: no verdict yet
    assert runs(world)["status"] == probe.PASS
    with edit(world.capture) as conn:
        conn.execute("UPDATE dream_runs SET started_at=?", (iso(3),))  # newest run not ok and older than the schedule
    assert runs(world)["status"] == probe.FAIL
    add_runs(world, ["ok"], hours_ago=0.1)  # a later healthy run clears it
    assert runs(world)["status"] == probe.PASS
    add_runs(world, ["failed"] * 2, hours_ago=2)  # three failures of four runs: the share rule applies from 4 runs
    assert runs(world)["status"] == probe.FAIL and runs(world)["data"]["total"] == 4


def drain(world, **cfg):
    (check,) = probe.check_dream_drain(world.capture, NOW, {**CFG, **cfg})
    return check


def age_waiting(world, hours):
    with edit(world.capture) as conn:
        conn.execute("UPDATE dream_captures SET last_activity_at=? WHERE state='captured'", (iso(hours),))


def set_capture(world, state, hours_ago, turns=3):
    with edit(world.capture) as conn:
        conn.execute("INSERT INTO dream_captures (state, captured_at, last_activity_at, turn_count, updated_at) VALUES (?,?,?,?,?)",
                     (state, iso(hours_ago), iso(hours_ago), turns, iso(hours_ago)))


def test_drain_is_red_when_work_waits_and_nothing_reached_a_terminal_state_in_the_window(world):
    age_waiting(world, 5)
    set_capture(world, "applied", 20)  # an exit, but older than the 12 h window
    check = drain(world)  # the fixture already holds one waiting capture
    assert check["status"] == probe.FAIL and check["data"] == {"exited": 0, "waiting_now": 1}, check


def test_drain_is_green_with_an_exit_in_the_window_or_with_nothing_waiting(world):
    for state in ("applied", "skipped", "quarantined"):
        set_capture(world, state, 3)
    assert drain(world)["status"] == probe.PASS and drain(world)["data"]["exited"] == 3
    with edit(world.capture) as conn:
        conn.execute("DELETE FROM dream_captures WHERE state IN ('captured','applied','skipped','quarantined')")
    set_capture(world, "applied", 30)
    assert drain(world)["status"] == probe.PASS, "an empty queue is not a stall"


def test_a_young_waiting_capture_after_a_quiet_window_is_not_a_stall(world):
    age_waiting(world, 1.5)
    assert drain(world)["status"] == probe.PASS
    age_waiting(world, 2.5)
    assert drain(world)["status"] == probe.FAIL


def test_drain_ignores_captures_below_the_scheduling_threshold(world):
    age_waiting(world, 5)
    set_capture(world, "skipped", 1, turns=1)  # the below-two-turns sweep is not throughput
    set_capture(world, "captured", 30, turns=1)
    assert drain(world)["status"] == probe.FAIL  # the fixture's real waiting capture still has no exit


def test_the_probe_names_the_stall_when_runs_are_abandoned_and_nothing_drains(world, hooks):
    canaries = world.tmp / "c.json"
    canaries.write_text(json.dumps({"canaries": []}), encoding="utf-8")
    add_runs(world, ["abandoned"] * 6)
    age_waiting(world, 5)
    report = probe.run_probe(world.db, workspace=world.tmp, canaries_path=canaries, transcripts_db=world.transcripts,
                             capture_db=world.capture, now=NOW, cfg=CFG, recallers={})
    assert {"dreaming.runs", "dreaming.drain"} <= set(report["failures"]), report["failures"]


def test_no_dreaming_runs_at_all_is_not_a_failure_here(world):
    assert runs(world)["status"] == probe.PASS  # scheduled_slots in operational_review owns "no runs"


def test_an_unreadable_run_table_is_a_fail_not_a_crash(world):
    with edit(world.capture) as conn:
        conn.execute("DROP TABLE dream_runs")
    out = probe._guard("dreaming.runs", probe.check_dream_runs, world.capture, NOW, CFG)
    assert out[0]["status"] == probe.FAIL and "could not run" in out[0]["detail"]


def finish(job="steward", hours_ago=1.0, **extra):
    return json.dumps({"event": "job_finish", "ts": iso(hours_ago).replace("+00:00", "Z"), "job": job,
                       "exit_code": 0, "outcome": "ok", **extra})


def steward(tmp_path, *lines):
    log = tmp_path / "steward.log"
    log.write_text("\n".join(["[MemoryMaster] steward cycle: {}", *lines, "not json {"]) + "\n", encoding="utf-8")
    (check,) = probe.check_steward_recency(log, NOW, CFG)
    return check


def test_steward_recency_is_green_with_a_recent_completed_cycle(tmp_path):
    check = steward(tmp_path, finish(hours_ago=20), finish(hours_ago=5))
    assert check["status"] == probe.PASS and check["data"]["age_h"] == 5.0, check


def test_steward_recency_is_red_after_two_missed_schedule_slots(tmp_path):
    assert steward(tmp_path, finish(hours_ago=13))["status"] == probe.FAIL  # schedule 6 h, bound 12 h
    assert steward(tmp_path, finish(hours_ago=11))["status"] == probe.PASS


def test_only_a_completed_steward_cycle_counts(tmp_path):
    lines = [finish(hours_ago=1, exit_code=1, outcome="failed"), finish("dream", hours_ago=1),
             finish(hours_ago=2, outcome="error"), json.dumps({"event": "job_start", "ts": iso(0.5), "job": "steward"})]
    check = steward(tmp_path, *lines, finish(hours_ago=30))
    assert check["status"] == probe.FAIL and check["data"]["age_h"] == 30.0, check


def test_a_missing_steward_log_is_red(tmp_path):
    (check,) = probe.check_steward_recency(tmp_path / "nope.log", NOW, CFG)
    assert check["status"] == probe.FAIL and check["data"]["newest"] == "none"
    assert "no completed steward cycle found" in check["detail"] and "None" not in check["detail"]


def test_runs_beyond_five_hundred_are_still_counted(world):
    with edit(world.capture) as conn:
        conn.executemany("INSERT INTO dream_runs VALUES (?,?,?,?,?,?,?,?,?)",
                         [(f"b{i}", "abandoned" if i % 4 else "ok", 0, "m", "m", iso(0.01 + i / 1000), iso(1), iso(1), "{}")
                          for i in range(576)])  # 48 h of five-minute ticks
    check = runs(world)
    assert check["data"]["total"] == 576 and check["data"]["abandoned"] == 432 and check["status"] == probe.FAIL


# --------------------------------------------------------------------------- G-6 composite scenario

def test_the_probe_is_red_when_hooks_drifted_and_dreaming_fails_although_every_store_is_fresh(world, hooks):
    """audit repro G-6: fresh stores, hooks not the templates, every dream run failing. It used to pass green."""
    canaries = world.tmp / "c.json"
    canaries.write_text(json.dumps({"canaries": []}), encoding="utf-8")
    (hooks["claude"] / "memorymaster-x.py").write_text("print('stale')\n", encoding="utf-8")
    add_runs(world, ["failed"] * 10)
    log = world.tmp / "steward.log"
    log.write_text(finish(hours_ago=1) + "\n", encoding="utf-8")

    def run(**extra):
        return probe.run_probe(world.db, workspace=world.tmp, canaries_path=canaries, transcripts_db=world.transcripts,
                               capture_db=world.capture, now=NOW, cfg=CFG, recallers={}, **extra)

    report = run(hook_dirs={"claude": hooks["claude"]}, hook_allowlist=hooks["allow"], steward_log=log)
    assert report["status"] == probe.FAIL
    assert {"dreaming.runs", "hooks.template_drift"} <= set(report["failures"]), report["failures"]
    assert "steward.recency" not in report["failures"]
    assert not {"hooks.template_drift", "steward.recency"} & {c["name"] for c in run()["checks"]}, "opt-in: no dirs, no read"

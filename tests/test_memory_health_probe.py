"""Contracts of the daily health probe (T-2121): it must fail on a broken user outcome, never write the DB,
and alert once per day. Every fixture is a temp DB; the live DB and the live services are never touched."""

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
            conn.execute("CREATE TABLE dream_captures (id INTEGER PRIMARY KEY, state TEXT, captured_at TEXT)")
            conn.execute("INSERT INTO dream_captures (state, captured_at) VALUES ('captured', ?)", (iso(2),))

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
        conn.execute("UPDATE dream_captures SET captured_at=?", (iso(24 * 30),))
    assert world.ingest_checks()["ingest.capture_queue"]["status"] == "FAIL"  # one very old capture
    with edit(world.capture) as conn:
        conn.execute("UPDATE dream_captures SET captured_at=?", (iso(1),))
        conn.executemany("INSERT INTO dream_captures (state, captured_at) VALUES ('captured', ?)", [(iso(1),)] * 1100)
    assert world.ingest_checks()["ingest.capture_queue"]["status"] == "FAIL"  # many fresh ones
    with edit(world.capture) as conn:
        conn.execute("UPDATE dream_captures SET state='applied'")
    assert world.ingest_checks()["ingest.capture_queue"]["status"] == "PASS"


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

def test_retention_fails_when_verbatim_is_outside_its_bounds(world):
    assert probe.check_retention(world.db)[0]["status"] == "PASS"
    with world.rw() as conn:
        conn.execute("INSERT INTO verbatim_memories (session_id, role, content, timestamp) VALUES ('old','user','x',?)", (iso(24 * 3000),))
    result = probe.check_retention(world.db)[0]
    assert result["status"] == "FAIL" and result["data"]["candidate_rows"] >= 1


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

def failing_report(*names, day="2026-11-03"):
    return {"generated_at": f"{day}T09:15:00+00:00", "failures": list(names), "status": "FAIL" if names else "PASS",
            "checks": [{"name": n, "status": "FAIL", "detail": f"{n} is broken"} for n in names] + [{"name": "ok", "status": "PASS", "detail": ""}]}


@pytest.fixture
def orchestrators(tmp_path):
    path = tmp_path / "orchestrators.json"
    path.write_text(json.dumps({"fleet": {"run_id": "run_fixture"}}), encoding="utf-8")
    return path


def test_alert_is_sent_once_per_day_unless_the_failure_set_changes(tmp_path, orchestrators):
    sent: list[tuple] = []
    send = lambda run, subject, body: sent.append((run, subject, body))  # noqa: E731
    out = tmp_path / "health"
    out.mkdir()
    first = probe.maybe_alert(failing_report("a", "b"), out, orchestrators, sender=send)
    assert first["sent"] and sent[0][:2] == ("run_fixture", "[MM-SALUD] 2 fallas")
    assert not probe.maybe_alert(failing_report("b", "a"), out, orchestrators, sender=send)["sent"], "same set, same day"
    assert probe.maybe_alert(failing_report("a", "b", "c"), out, orchestrators, sender=send)["sent"], "set changed"
    assert probe.maybe_alert(failing_report("a", "b", "c", day="2026-11-04"), out, orchestrators, sender=send)["sent"], "next day"
    assert len(sent) == 3
    assert not probe.maybe_alert(failing_report(), out, orchestrators, sender=send)["sent"], "nothing failing"


def test_a_failed_send_is_retried_next_run_and_never_raises(tmp_path, orchestrators):
    def broken(*_):
        raise RuntimeError("orca down")

    out = tmp_path
    result = probe.maybe_alert(failing_report("a"), out, orchestrators, sender=broken)
    assert not result["sent"] and "send failed" in result["reason"]
    assert not (out / "alert-state.json").exists()
    assert probe.maybe_alert(failing_report("a"), out, orchestrators, sender=lambda *_: None)["sent"]


def test_alert_body_carries_criteria_and_a_digit_free_voz_line():
    body = probe.alert_body(failing_report("ingest.verbatim", "retention.within_bounds"))
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

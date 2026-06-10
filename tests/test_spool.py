"""Spool core + drainer tests (P1 WAL-discipline spec step 7, §2.2/§2.4).

WHY: ~12 concurrent writers share one 3.47 GB SQLite file and real btree
corruption already happened (2026-06-05). The spool lets high-frequency
ambient writers (recall access records, Stop-hook verbatim, dream bridge)
append a ~10 ms JSONL line instead of opening the DB per event; the steward
drains those lines through the NORMAL service paths. These tests pin the
load-bearing guarantees: the envelope wire protocol, rename-before-read
isolation (writers never race the reader), quarantine-never-drop, replay
idempotency (a crashed drain must not duplicate claims), and — sacred —
that the sensitivity filter fires on drained ingest lines exactly as it
does for a live MCP call, because the drainer reuses ``svc.ingest``.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from memorymaster import spool
from memorymaster.govern.jobs import spool_drain
from memorymaster.models import CitationInput
from memorymaster.service import MemoryService
from memorymaster.storage import SQLiteStore


@pytest.fixture()
def svc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MemoryService:
    # Redirect the spool root into the test tree — tests must never write to
    # the real ~/.memorymaster/spool of the machine running them.
    monkeypatch.setenv(spool.ENV_SPOOL_DIR, str(tmp_path / "spool-root"))
    # run_cycle's integrity phase vacuums a snapshot — keep it out of ~.
    monkeypatch.setenv("MEMORYMASTER_SNAPSHOT_DIR", str(tmp_path / "snaps"))
    monkeypatch.delenv("QDRANT_URL", raising=False)
    service = MemoryService(tmp_path / "spool.db")
    service.init_db()
    return service


def _db(svc: MemoryService) -> str:
    return str(svc.store.db_path)


def _claim_count(svc: MemoryService) -> int:
    with svc.store.connect() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0])


def _ingest_payload(text: str) -> dict[str, object]:
    return {
        "text": text,
        "scope": "project:memorymaster",
        "source_agent": "spool-test",
        "citations": [{"source": "session://stop-hook", "locator": "turn-1"}],
    }


def test_envelope_round_trip(svc: MemoryService) -> None:
    """The JSONL envelope is the only wire protocol in the P1 design — its
    shape (v/op/ts/idempotency_key/payload) and the {pid}-{date}.jsonl file
    naming must survive a write+read round trip byte-exactly, because step-8/9
    writers and this drainer only meet through this file format."""
    path = spool.append(
        _db(svc), "access", {"claim_ids": [1, 2], "query_hash": "abc"},
        idempotency_key="k-1",
    )
    assert re.fullmatch(r"\d+-\d{8}\.jsonl", path.name)
    assert path.parent == spool.spool_dir_for(_db(svc))
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    envelope = json.loads(lines[0])
    assert envelope["v"] == spool.SPOOL_VERSION == 1
    assert envelope["op"] == "access"
    assert envelope["idempotency_key"] == "k-1"
    assert envelope["payload"] == {"claim_ids": [1, 2], "query_hash": "abc"}
    assert envelope["ts"]  # iso8601, set by append


def test_spool_lives_outside_db_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec §2.2: the spool sits outside the DB directory (and the OneDrive
    tree) so spool I/O can never contend with — or be synced over — the live
    DB files. The spool dir must derive from env/home, NEVER from the DB's
    own directory — a spool file next to memorymaster.db would reintroduce
    the exact failure class P1 exists to kill."""
    monkeypatch.setenv(spool.ENV_SPOOL_DIR, str(tmp_path / "spool-root"))
    db = tmp_path / "dbdir" / "memorymaster.db"
    spool_dir = spool.spool_dir_for(db)
    assert db.parent.resolve() not in spool_dir.resolve().parents
    assert spool_dir.parent == tmp_path / "spool-root"
    # Without the env override the root is ~/.memorymaster/spool — outside
    # any project checkout (and therefore outside the OneDrive-synced tree).
    monkeypatch.delenv(spool.ENV_SPOOL_DIR)
    assert spool.spool_dir_for(db).parent == Path.home() / ".memorymaster" / "spool"


def test_unknown_op_rejected_at_append_time(svc: MemoryService) -> None:
    """Writers must fail loudly on a typo'd op — a silently-accepted unknown
    op would be quarantined at drain time hours later, losing the signal."""
    with pytest.raises(ValueError):
        spool.append(_db(svc), "frobnicate", {})


def test_rename_before_read_isolation(svc: MemoryService) -> None:
    """Spec §2.2: the drainer renames a file before reading it so writers
    never race the reader. After claim_files() the original name is gone —
    a writer appending again creates a FRESH pending file, and the claimed
    .draining file is untouched by that new write."""
    spool.append(_db(svc), "access", {"claim_ids": [1]})
    claimed = spool.claim_files(_db(svc))
    assert len(claimed) == 1
    assert claimed[0].name.endswith(spool.DRAINING_SUFFIX)
    spool_dir = spool.spool_dir_for(_db(svc))
    assert list(spool_dir.glob("*.jsonl")) == []  # original name retired

    # A post-claim writer append lands in a new pending file, not the claimed one.
    before = claimed[0].read_text(encoding="utf-8")
    spool.append(_db(svc), "access", {"claim_ids": [2]})
    pending = list(spool_dir.glob("*.jsonl"))
    assert len(pending) == 1
    assert claimed[0].read_text(encoding="utf-8") == before


def test_unknown_op_and_garbage_quarantined_not_dropped(svc: MemoryService) -> None:
    """Spec §2.2: unknown op/v or unparseable lines go to quarantine/ —
    NEVER dropped silently. A spooled write is a user's memory signal; the
    drainer has no right to discard what it cannot replay."""
    spool_dir = spool.spool_dir_for(_db(svc))
    spool_dir.mkdir(parents=True, exist_ok=True)
    bogus_op = json.dumps(spool.make_envelope("access", {})).replace("access", "frobnicate")
    garbage = "{not json at all"
    (spool_dir / "999-20260101.jsonl").write_text(bogus_op + "\n" + garbage + "\n", encoding="utf-8")

    result = spool_drain.run(svc)
    assert result["drained"] == 0
    assert result["quarantined"] == 2
    qfiles = list(spool.quarantine_dir_for(_db(svc)).glob("*.jsonl"))
    assert len(qfiles) == 1
    records = [json.loads(line) for line in qfiles[0].read_text(encoding="utf-8").splitlines()]
    assert {r["raw"] for r in records} == {bogus_op, garbage}
    assert any("unknown_op" in r["reason"] for r in records)
    assert any(r["reason"] == "invalid_json" for r in records)
    # Nothing pending after the drain — quarantine is the only residue.
    assert spool.pending_depth(_db(svc)) == {"files": 0, "lines": 0}


def test_double_drain_of_same_ingest_line_yields_one_claim(svc: MemoryService) -> None:
    """Spec §2.4: replay must be idempotent. A crashed drain (or a duplicated
    envelope) re-replays the same ingest line; svc.ingest's idempotency_key
    dedup must collapse it to ONE claim or the spool would silently multiply
    memories under failure."""
    payload = _ingest_payload("The steward batch_limit threads into all cycle jobs")
    spool.append(_db(svc), "ingest", payload, idempotency_key="spool-idem-1")
    first = spool_drain.run(svc)
    assert first["drained"] == 1
    assert first["lag_seconds"] >= 0
    assert _claim_count(svc) == 1

    spool.append(_db(svc), "ingest", payload, idempotency_key="spool-idem-1")
    second = spool_drain.run(svc)
    assert second["drained"] == 1  # replayed fine — dedup returned the existing claim
    assert second["quarantined"] == 0
    assert _claim_count(svc) == 1


def test_crashed_drain_leftover_draining_file_is_reclaimed(svc: MemoryService) -> None:
    """A drain that dies between rename and replay leaves a .draining file.
    The next run must re-claim and replay it — otherwise a crash strands
    writes forever, violating the §5 'no write is ever stranded' guarantee."""
    spool.append(_db(svc), "ingest", _ingest_payload("Qdrant drift threshold defaults to one hundred points"))
    claimed = spool.claim_files(_db(svc))  # simulate: renamed, then crashed
    assert claimed and claimed[0].exists()

    result = spool_drain.run(svc)
    assert result["drained"] == 1
    assert _claim_count(svc) == 1
    assert spool.pending_depth(_db(svc)) == {"files": 0, "lines": 0}


def test_sensitivity_filter_fires_on_drained_ingest(svc: MemoryService) -> None:
    """RED-BAR (spec step 7 + .claude/rules/sensitivity-filter.md): the spool
    is a NEW ingest path, and every ingest path is default-deny until the
    filter is wired. A credential spooled by an ambient writer must land
    REDACTED at rest — if this test fails, the spool is a secret-exfiltration
    channel and must not ship."""
    secret = "sk-LiveSecret1234567890abcd"
    spool.append(
        _db(svc), "ingest",
        _ingest_payload(f"Deploy script authenticates with OPENAI key {secret} from env"),
        idempotency_key="spool-redbar-1",
    )
    result = spool_drain.run(svc)
    assert result["drained"] == 1

    with svc.store.connect() as conn:
        texts = [r[0] for r in conn.execute("SELECT text FROM claims").fetchall()]
        events = conn.execute(
            "SELECT COUNT(*) FROM events WHERE event_type = 'policy_decision' AND details = 'sensitive_redaction_applied'"
        ).fetchone()[0]
    assert len(texts) == 1
    assert secret not in texts[0]
    assert "[REDACTED:openai_key]" in texts[0]
    assert events == 1


def test_access_and_feedback_lines_replay_through_normal_paths(svc: MemoryService) -> None:
    """Spec F9 fix: making the recall hook read-only is only acceptable
    because NO signal is lost — spooled access lines must reach
    record_accesses_batch (tier/decay input) and feedback lines must reach
    FeedbackTracker (quality scoring), exactly as the RW path did."""
    claim = svc.ingest(
        text="The recall hook reads claims through a read-only connection",
        citations=[CitationInput(source="session://chat", locator="turn-1")],
    )
    spool.append(_db(svc), "access", {"claim_ids": [claim.id], "query_hash": "q1"})
    spool.append(_db(svc), "feedback", {"claim_ids": [claim.id], "query_text": "how does recall work"})

    result = spool_drain.run(svc)
    assert result["drained"] == 2
    assert result["by_op"] == {"access": 1, "feedback": 1}

    with svc.store.connect() as conn:
        access_count = conn.execute(
            "SELECT access_count FROM claims WHERE id = ?", (claim.id,)
        ).fetchone()[0]
        feedback_rows = conn.execute(
            "SELECT COUNT(*) FROM usage_feedback WHERE claim_id = ?", (claim.id,)
        ).fetchone()[0]
    assert access_count == 1
    assert feedback_rows == 1


def test_verbatim_line_lands_in_verbatim_memories(svc: MemoryService) -> None:
    """Spec §2.3: Stop-hook verbatim turns spool instead of opening the 3.47 GB
    DB per event — but the drained row must land in verbatim_memories through
    store_verbatim (its sensitivity + dedup logic intact), or the latency win
    would cost us the verbatim record."""
    content = "User asked about the WAL checkpoint discipline rollout plan"
    spool.append(_db(svc), "verbatim", {
        "session_id": "sess-1",
        "role": "user",
        "content": content,
        "scope": "project:memorymaster",
        "source_agent": "stop-hook",
    })
    result = spool_drain.run(svc)
    assert result["drained"] == 1
    with svc.store.connect() as conn:
        rows = conn.execute(
            "SELECT session_id, role, content FROM verbatim_memories"
        ).fetchall()
    assert [(r[0], r[1], r[2]) for r in rows] == [("sess-1", "user", content)]


def test_poison_line_quarantined_without_blocking_others(svc: MemoryService) -> None:
    """One bad line (here: empty text → svc.ingest raises) must quarantine
    and continue — a single poison envelope must never wedge the drain and
    dam up every later write behind it."""
    spool.append(_db(svc), "ingest", {"text": "", "citations": []})
    spool.append(_db(svc), "ingest", _ingest_payload("Spool drain isolates poison lines per envelope"))

    result = spool_drain.run(svc)
    assert result["drained"] == 1
    assert result["quarantined"] == 1
    assert _claim_count(svc) == 1
    qfiles = list(spool.quarantine_dir_for(_db(svc)).glob("*.jsonl"))
    assert len(qfiles) == 1
    record = json.loads(qfiles[0].read_text(encoding="utf-8").splitlines()[0])
    assert record["reason"].startswith("replay_error:")


def test_run_cycle_includes_spool_drain_phase(svc: MemoryService) -> None:
    """The drain is wired as a steward-cycle phase (spec §2.4) — ambient
    writes have at most one cycle of visibility lag, with the drained/
    quarantined/lag metrics surfaced in the cycle result (§2.10)."""
    spool.append(_db(svc), "ingest", _ingest_payload("Cycle phase drains the spool automatically"))
    result = svc.run_cycle()
    phase = result["spool_drain"]
    assert phase["drained"] == 1
    assert phase["quarantined"] == 0
    assert "lag_seconds" in phase
    assert _claim_count(svc) == 1


def test_cli_drain_spool_json(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """`memorymaster drain-spool --json` is the one-shot operator/rollback
    path (spec §5): after turning the flag off, residue must drain via this
    command with a parseable envelope and exit 0 — no write ever stranded."""
    from memorymaster.surfaces.cli import main

    monkeypatch.setenv(spool.ENV_SPOOL_DIR, str(tmp_path / "spool-root"))
    db = tmp_path / "cli-spool.db"
    SQLiteStore(db).init_db()
    spool.append(str(db), "ingest", _ingest_payload("Rollback drains spool residue through the CLI"))

    rc = main(["--db", str(db), "--json", "drain-spool"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["ok"] is True
    assert out["data"]["drained"] == 1
    assert out["data"]["quarantined"] == 0

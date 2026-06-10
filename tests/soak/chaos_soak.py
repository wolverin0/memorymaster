"""Chaos soak harness — the P1 WAL-discipline exit gate (spec §4, step 12).

WHY this exists: the 2026-06-05 btree corruption happened under ~12 concurrent
writer processes sharing one SQLite file, with processes dying mid-write. Unit
tests cannot reproduce that failure mode — only a fleet of REAL OS processes,
hard-killed mid-flight (`Popen.kill()` == `TerminateProcess` == `taskkill /F`),
can. The gate (spec §4): across ≥20 kill rounds, in BOTH flag modes
(MEMORYMASTER_WAL_DISCIPLINE off = legacy regression guard, on = new regime):

- ``PRAGMA quick_check`` returns ``ok`` after every round (no btree damage),
- ``PRAGMA foreign_key_check`` returns 0 rows (no orphan collateral),
- every ACKED write (journaled to a per-writer ledger AFTER the commit/append
  returned) is present in the DB exactly once after the final drain — proves
  kill-safety AND replay idempotency of the spool,
- busy-error counts are recorded both ways (§7(d) tripwire input).

Collection: this file deliberately does NOT match ``test_*.py`` so the default
``pytest tests/`` run never picks it up; the tests are additionally marked
``soak`` (registered in pytest.ini). Run explicitly (spec §3: gated run happens
after merge, against a slice of the live DB — never the live file):

    pwsh scripts/run_chaos_soak.ps1            # builds slice + runs both modes
    python -m pytest "tests/soak/chaos_soak.py" -q -p no:cacheprovider

Tunables (env): MM_SOAK_ROUNDS (default 20), MM_SOAK_ROUND_SECS (default 60),
MM_SOAK_DB_SLICE (path to a fixture slice; absent -> small synthetic fixture).

This same file is also the writer-process entry point (``--role``, loops in
``soak_writers.py``) and the slice-builder entry point (``--build-slice``,
implementation in ``soak_slice.py``).
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:  # writer subprocesses + any pytest import mode
    sys.path.insert(0, str(_HERE))

from soak_slice import build_slice  # noqa: E402
from soak_writers import ROLE_LOOPS, Ledger  # noqa: E402

ENV_ROUNDS = "MM_SOAK_ROUNDS"
ENV_ROUND_SECS = "MM_SOAK_ROUND_SECS"
ENV_DB_SLICE = "MM_SOAK_DB_SLICE"

DEFAULT_ROUNDS = 20
DEFAULT_ROUND_SECS = 60.0
KILL_MIN, KILL_MAX = 2, 4  # spec §4: kill 2-4 writers per round

# The simulated fleet, matching the real writer shape (spec §4): 6 MCP-style
# ingest loops, 2 recall loops, 1 Stop-hook loop, 1 dream-bridge loop,
# 1 steward run_cycle loop, 1 merge-db loop = 12 writers.
FLEET: tuple[tuple[str, int], ...] = (
    ("ingest", 6),
    ("recall", 2),
    ("stophook", 1),
    ("dream", 1),
    ("steward", 1),
    ("merge", 1),
)

# Env that must NOT leak into writer subprocesses: real provider keys would
# make svc.ingest embed via live APIs and the steward call real LLMs; a real
# QDRANT_URL would push soak claims into the production vector store.
SANITIZE_ENV = (
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "QDRANT_URL",
    "MEMORYMASTER_DEFAULT_DB",
    "MEMORYMASTER_INITDB_FASTPATH",
)


# ---------------------------------------------------------------------------
# Orchestrator: fixture, fleet management, gates, reconciliation
# ---------------------------------------------------------------------------

@dataclass
class WriterProc:
    role: str
    writer_id: int
    spawn_n: int
    proc: subprocess.Popen


@dataclass
class SoakRun:
    db: Path
    run_dir: Path
    env: dict[str, str]
    rounds: int
    round_secs: float
    writers: list[WriterProc] = field(default_factory=list)
    spawn_counter: int = 0
    report: dict[str, object] = field(default_factory=dict)

    @property
    def ledger_dir(self) -> Path:
        return self.run_dir / "ledgers"


def _seed_synthetic_fixture(db: Path) -> None:
    """Small schema-identical fixture for smoke/dev runs without a live slice."""
    from memorymaster.models import CitationInput
    from memorymaster.service import MemoryService
    from memorymaster.recall.verbatim_store import ensure_verbatim_schema

    svc = MemoryService(db)
    svc.init_db()
    for i in range(40):
        svc.ingest(
            f"Seed claim {i}: sqlite wal checkpoint discipline topic number {i}.",
            [CitationInput(source="soak-seed", locator=str(i))],
            idempotency_key=f"soak-seed-{i}",
            scope="project:soak",
            source_agent="chaos-soak",
        )
    ensure_verbatim_schema(str(db))


def _prepare_db(run_dir: Path) -> Path:
    db = run_dir / "soak.db"
    slice_path = os.environ.get(ENV_DB_SLICE, "").strip()
    if slice_path:
        source = Path(slice_path)
        if not source.exists():
            raise FileNotFoundError(f"{ENV_DB_SLICE} points at a missing slice: {source}")
        shutil.copy2(source, db)  # the slice is checkpoint-truncated: no -wal/-shm
    else:
        _seed_synthetic_fixture(db)
    return db


def _writer_env(base_env: dict[str, str], run_dir: Path, wal_discipline: bool) -> dict[str, str]:
    env = dict(base_env)
    for key in SANITIZE_ENV:
        env.pop(key, None)
    env["MEMORYMASTER_SPOOL_DIR"] = str(run_dir / "spool")
    env["MEMORYMASTER_SNAPSHOT_DIR"] = str(run_dir / "snapshots")
    if wal_discipline:
        env["MEMORYMASTER_WAL_DISCIPLINE"] = "1"
    else:
        env.pop("MEMORYMASTER_WAL_DISCIPLINE", None)
    return env


def _spawn(run: SoakRun, role: str, writer_id: int) -> WriterProc:
    run.spawn_counter += 1
    ledger = run.ledger_dir / f"{role}-{writer_id}-spawn{run.spawn_counter}.jsonl"
    log = run.run_dir / "logs" / f"{role}-{writer_id}-spawn{run.spawn_counter}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--role", role,
        "--db", str(run.db),
        "--ledger", str(ledger),
        "--writer-id", str(writer_id),
    ]
    if role == "merge":
        cmd += ["--sibling", str(run.run_dir / f"sibling-{writer_id}.db")]
    proc = subprocess.Popen(  # noqa: S603 - spawning our own harness entry
        cmd,
        env=run.env,
        cwd=str(run.run_dir),
        stdout=subprocess.DEVNULL,
        stderr=open(log, "a", encoding="utf-8"),
    )
    return WriterProc(role=role, writer_id=writer_id, spawn_n=run.spawn_counter, proc=proc)


def _spawn_fleet(run: SoakRun) -> None:
    writer_id = 0
    for role, count in FLEET:
        for _ in range(count):
            writer_id += 1
            run.writers.append(_spawn(run, role, writer_id))


def _kill_and_respawn(run: SoakRun, n_victims: int, rng: random.Random) -> list[str]:
    """Hard-kill (TerminateProcess == taskkill /F) random writers mid-flight."""
    victims = rng.sample(range(len(run.writers)), k=min(n_victims, len(run.writers)))
    killed: list[str] = []
    for idx in victims:
        wp = run.writers[idx]
        wp.proc.kill()
        wp.proc.wait(timeout=30)
        killed.append(f"{wp.role}-{wp.writer_id}")
        run.writers[idx] = _spawn(run, wp.role, wp.writer_id)
    return killed


def _respawn_dead(run: SoakRun) -> int:
    """Respawn writers that exited on their own (counted, not gated)."""
    dead = 0
    for idx, wp in enumerate(run.writers):
        if wp.proc.poll() is not None:
            dead += 1
            run.writers[idx] = _spawn(run, wp.role, wp.writer_id)
    return dead


def _stop_fleet(run: SoakRun) -> None:
    for wp in run.writers:
        wp.proc.kill()
    for wp in run.writers:
        try:
            wp.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pass


def _quick_check(db: Path) -> list[str]:
    from memorymaster._storage_shared import connect_ro

    conn = connect_ro(db, query_ms=60000)
    try:
        return [str(row[0]) for row in conn.execute("PRAGMA quick_check").fetchall()]
    finally:
        conn.close()


def _fk_orphans(db: Path) -> int:
    from memorymaster._storage_shared import connect_ro

    conn = connect_ro(db, query_ms=60000)
    try:
        return len(conn.execute("PRAGMA foreign_key_check").fetchall())
    finally:
        conn.close()


def _wal_bytes(db: Path) -> int:
    wal = Path(str(db) + "-wal")
    return wal.stat().st_size if wal.exists() else 0


def _final_drain(db: Path) -> dict[str, object]:
    """Drain any spool residue through the normal paths (rollback guarantee §5)."""
    from memorymaster import spool
    from memorymaster.jobs import spool_drain
    from memorymaster.service import MemoryService

    svc = MemoryService(db)
    last: dict[str, object] = {}
    for _ in range(6):
        last = spool_drain.run(svc)
        depth = spool.pending_depth(db)
        if depth["files"] == 0 and depth["lines"] == 0:
            break
    return last


def _read_ledgers(ledger_dir: Path) -> dict[str, object]:
    ingest_keys: set[str] = set()
    verbatim: dict[str, tuple[str, str]] = {}
    merge_texts: dict[str, str] = {}
    counts = {"busy": 0, "error": 0, "recall": 0, "cycle": 0}
    for path in sorted(ledger_dir.glob("*.jsonl")):
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not raw.strip():
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue  # torn final line of a killed writer: op was never acked
            kind = rec.get("kind")
            if kind == "ingest" and rec.get("key"):
                ingest_keys.add(str(rec["key"]))
            elif kind == "verbatim" and rec.get("key"):
                verbatim[str(rec["key"])] = (str(rec.get("session_id")), str(rec.get("content")))
            elif kind == "merge" and rec.get("key"):
                merge_texts[str(rec["key"])] = str(rec.get("text"))
            elif kind in counts:
                counts[kind] += 1
    return {
        "ingest_keys": ingest_keys,
        "verbatim": verbatim,
        "merge_texts": merge_texts,
        "counts": counts,
    }


def _reconcile(db: Path, ledger_dir: Path) -> dict[str, object]:
    """Every acked op must be in the DB exactly once (merge: at least once).

    Exactly-once for ingest proves BOTH kill-safety (nothing acked was lost)
    and replay idempotency (a crashed-then-re-drained spool line, or a
    re-acked key after respawn, never duplicates a claim).
    """
    from memorymaster._storage_shared import connect_ro

    acked = _read_ledgers(ledger_dir)
    lost: list[str] = []
    duplicated: list[str] = []
    conn = connect_ro(db, query_ms=60000)
    try:
        for key in sorted(acked["ingest_keys"]):
            n = conn.execute(
                "SELECT COUNT(*) FROM claims WHERE idempotency_key = ?", (key,)
            ).fetchone()[0]
            if n == 0:
                lost.append(f"ingest:{key}")
            elif n > 1:
                duplicated.append(f"ingest:{key}")
        for key, (session_id, content) in sorted(acked["verbatim"].items()):
            n = conn.execute(
                "SELECT COUNT(*) FROM verbatim_memories WHERE session_id = ? AND content = ?",
                (session_id, content),
            ).fetchone()[0]
            if n == 0:
                lost.append(f"verbatim:{key}")
            elif n > 1:
                duplicated.append(f"verbatim:{key}")
        # Merge re-keys idempotency on the target (db_merge._NON_PORTABLE_COLS),
        # so reconcile acked merges by their unique text: at least once.
        for key, text in sorted(acked["merge_texts"].items()):
            n = conn.execute("SELECT COUNT(*) FROM claims WHERE text = ?", (text,)).fetchone()[0]
            if n == 0:
                lost.append(f"merge:{key}")
    finally:
        conn.close()
    return {
        "acked_ingest": len(acked["ingest_keys"]),
        "acked_verbatim": len(acked["verbatim"]),
        "acked_merge": len(acked["merge_texts"]),
        "counts": acked["counts"],
        "lost": lost,
        "duplicated": duplicated,
    }


def _run_soak(run_dir: Path, *, wal_discipline: bool) -> dict[str, object]:
    rounds = int(os.environ.get(ENV_ROUNDS, DEFAULT_ROUNDS))
    round_secs = float(os.environ.get(ENV_ROUND_SECS, DEFAULT_ROUND_SECS))
    run_dir.mkdir(parents=True, exist_ok=True)
    db = _prepare_db(run_dir)
    env = _writer_env(dict(os.environ), run_dir, wal_discipline)
    # The orchestrator's own gates/drain must see the writers' spool.
    os.environ["MEMORYMASTER_SPOOL_DIR"] = env["MEMORYMASTER_SPOOL_DIR"]
    os.environ["MEMORYMASTER_SNAPSHOT_DIR"] = env["MEMORYMASTER_SNAPSHOT_DIR"]

    run = SoakRun(db=db, run_dir=run_dir, env=env, rounds=rounds, round_secs=round_secs)
    rng = random.Random(os.getpid() ^ int(time.time()))
    round_reports: list[dict[str, object]] = []
    self_exits = 0
    _spawn_fleet(run)
    try:
        for round_no in range(1, rounds + 1):
            kill_at = round_secs * rng.uniform(0.25, 0.75)
            time.sleep(kill_at)
            killed = _kill_and_respawn(run, rng.randint(KILL_MIN, KILL_MAX), rng)
            time.sleep(max(0.0, round_secs - kill_at))
            self_exits += _respawn_dead(run)
            qc = _quick_check(db)
            orphans = _fk_orphans(db)
            round_reports.append(
                {
                    "round": round_no,
                    "killed": killed,
                    "quick_check": qc,
                    "fk_orphans": orphans,
                    "wal_bytes": _wal_bytes(db),
                }
            )
    finally:
        _stop_fleet(run)

    drain = _final_drain(db)
    final_qc = _quick_check(db)
    final_orphans = _fk_orphans(db)
    reconciliation = _reconcile(db, run.ledger_dir)
    sentinel = Path(str(db) + ".integrity-failed")
    report: dict[str, object] = {
        "mode": "wal_discipline_on" if wal_discipline else "wal_discipline_off",
        "rounds": round_reports,
        "self_exits_respawned": self_exits,
        "final_drain": drain,
        "final_quick_check": final_qc,
        "final_fk_orphans": final_orphans,
        "integrity_sentinel_present": sentinel.exists(),
        "reconciliation": reconciliation,
    }
    (run_dir / "soak-report.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )
    return report


def _assert_gates(report: dict[str, object]) -> None:
    """The P1 pass gate (spec §4): 0 quick_check failures, 0 FK orphans,
    0 lost acked writes — plus a non-vacuous activity floor."""
    bad_rounds = [
        r for r in report["rounds"]
        if r["quick_check"] != ["ok"] or r["fk_orphans"] != 0
    ]
    assert not bad_rounds, f"integrity gate failed in rounds: {bad_rounds}"
    assert report["final_quick_check"] == ["ok"], (
        f"final quick_check not ok: {report['final_quick_check']}"
    )
    assert report["final_fk_orphans"] == 0, (
        f"orphan FK rows after soak: {report['final_fk_orphans']}"
    )
    assert report["integrity_sentinel_present"] is False, (
        "integrity sentinel appeared — a cycle-time quick_check failed mid-soak"
    )
    rec = report["reconciliation"]
    assert rec["lost"] == [], f"LOST acked writes: {rec['lost'][:20]}"
    assert rec["duplicated"] == [], f"DUPLICATED acked writes: {rec['duplicated'][:20]}"
    assert rec["acked_ingest"] > 0, "vacuous soak: no ingest was ever acked"
    assert rec["acked_verbatim"] > 0, "vacuous soak: no verbatim turn was ever acked"


def _soak_test_body(tmp_path: Path, monkeypatch, *, wal_discipline: bool) -> None:
    for key in SANITIZE_ENV:
        monkeypatch.delenv(key, raising=False)
    if wal_discipline:
        monkeypatch.setenv("MEMORYMASTER_WAL_DISCIPLINE", "1")
    else:
        monkeypatch.delenv("MEMORYMASTER_WAL_DISCIPLINE", raising=False)
    report = _run_soak(tmp_path / "run", wal_discipline=wal_discipline)
    _assert_gates(report)


# ---------------------------------------------------------------------------
# The two gate tests (spec §4 matrix: flag OFF and flag ON)
# ---------------------------------------------------------------------------

try:  # pytest is absent when this file runs as a writer subprocess
    import pytest
except ImportError:  # pragma: no cover
    pytest = None  # type: ignore[assignment]

if pytest is not None:

    @pytest.mark.soak
    def test_chaos_soak_flag_off(tmp_path: Path, monkeypatch: "pytest.MonkeyPatch") -> None:
        """Flag OFF = the legacy v3.27 direct-write regime must survive the
        kill rounds unscathed.

        Intent: regression guard — if the soak fails here, the corruption
        class predates P1's changes and the WAL-discipline work neither
        caused nor masks it; if it passes here but fails flag-ON, P1 itself
        introduced the regression. The matrix makes the blame assignable.
        """
        _soak_test_body(tmp_path, monkeypatch, wal_discipline=False)

    @pytest.mark.soak
    def test_chaos_soak_flag_on(tmp_path: Path, monkeypatch: "pytest.MonkeyPatch") -> None:
        """Flag ON = the new regime (RO recall, ambient spool, steward drain)
        must survive the kill rounds with zero lost acked writes.

        Intent: this is the P1 exit gate (spec §4) — the dogfood flip to
        default-ON is only defensible if hard kills mid-spool-append and
        mid-drain provably lose nothing and never double-apply (idempotency
        under kill, grafted from the daemon draft's replay reasoning).
        """
        _soak_test_body(tmp_path, monkeypatch, wal_discipline=True)


# ---------------------------------------------------------------------------
# CLI entry: writer subprocess / slice builder
# ---------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="chaos soak writer / slice builder")
    parser.add_argument("--role", choices=sorted(ROLE_LOOPS))
    parser.add_argument("--db")
    parser.add_argument("--ledger")
    parser.add_argument("--writer-id", type=int, default=0)
    parser.add_argument("--sibling")
    parser.add_argument("--build-slice", action="store_true")
    parser.add_argument("--source")
    parser.add_argument("--dest")
    parser.add_argument("--max-claims", type=int, default=20000)
    parser.add_argument("--max-verbatim", type=int, default=50000)
    args = parser.parse_args(argv)

    if args.build_slice:
        if not args.source or not args.dest:
            parser.error("--build-slice requires --source and --dest")
        stats = build_slice(
            Path(args.source),
            Path(args.dest),
            max_claims=args.max_claims,
            max_verbatim=args.max_verbatim,
        )
        sys.stderr.write(f"slice built: {json.dumps(stats)}\n")
        return 0

    if not args.role or not args.db or not args.ledger:
        parser.error("writer mode requires --role, --db and --ledger")
    random.seed(os.getpid() ^ int(time.time_ns() & 0xFFFFFFFF))
    ledger = Ledger(Path(args.ledger))
    loop = ROLE_LOOPS[args.role]
    if args.role == "merge":
        loop(Path(args.db), ledger, args.writer_id, Path(args.sibling))
    else:
        loop(Path(args.db), ledger, args.writer_id)
    return 0  # pragma: no cover - loops never return


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

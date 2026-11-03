#!/usr/bin/env python3
"""Daily read-only health probe for MemoryMaster (T-2121).

Fails loudly when the *user outcome* breaks, not a proxy: stores stopped ingesting, recall returns
off-topic rows, core claims rank at the floor, the verbatim stop-hook skips storing, a service is down.
On 2026-11-03 all of those had been true for months while tests, latency and citation rate stayed green.

Read-only by construction: the authoritative DB is opened ``mode=ro`` + ``query_only``; recall runs with a
read-only ``MemoryService`` and its access spool, session-seen and metrics files are redirected to a
throwaway directory, so a canary query can never raise a claim's ``access_count`` (self-reinforcement).

Report: ``<out>/<UTC date>.json`` and ``<out>/latest.json``. Exit 1 on any FAIL. On FAIL one Orca message is
sent to the Fleet run, at most once per UTC day unless the set of failing checks changes (``--no-alert`` off).

    python scripts/memory_health_probe.py --db memorymaster.db --workspace . [--out DIR] [--no-alert]
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:  # `python scripts/x.py` puts scripts/, not the repo, on sys.path
    sys.path.insert(0, str(REPO))

# Every tunable lives here. Ages are hours.
CONFIG: dict = {
    "verbatim_max_age_h": 48,
    "transcripts_max_age_h": 72,
    # source_agent values grouped by family; max age of the newest claim of each family
    "claim_families": {
        "session": {"max_age_h": 72, "agents": ["claude-session", "codex-session", "opencode-session", "mcp-session"]},
        "hook": {"max_age_h": 72, "agents": ["llm-stop-hook", "agy-deterministic-hook", "precompact-residual"]},
        "dream-worker": {"max_age_h": 48, "agents": ["dream-worker"]},
    },
    "embeddings_max_age_h": 72,
    "embeddings_min_coverage": 0.90,  # share of confirmed+candidate claims that have an embedding
    "capture_backlog_max": 1000,  # dream_captures still in state 'captured'
    "capture_backlog_max_age_h": 168,  # age of the oldest one
    "canary_min_hit_rate": {"hook": 0.6, "mcp": 0.6},  # hit@5 per recall path
    "core_freshness_min": 1e-3,  # median freshness_score of core confirmed claims the canaries return
    "services": {"mcp_http": "http://127.0.0.1:8766/healthz", "dense": "http://127.0.0.1:8767/health"},
    "service_timeout_s": 4.0,
}

# One constant sentence: plain Spanish, past tense, no digits or ids (checked by wezbridge lint-voz-line).
VOZ = "Terminó la sonda diaria de salud y encontró fallas en lo que el sistema de memoria guarda o recuerda."
FAIL, PASS = "FAIL", "PASS"


# --------------------------------------------------------------------------- helpers

def ro_connect(path: str | Path) -> sqlite3.Connection:
    """Physically read-only connection; never creates a file."""
    uri = "file:" + quote(Path(path).resolve().as_posix(), safe="/:") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.execute("PRAGMA query_only=ON")
    return conn


def parse_ts(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _age_h(ts: datetime | None, now: datetime) -> float | None:
    return None if ts is None else round((now - ts).total_seconds() / 3600, 1)


def _result(name: str, ok: bool, detail: str, **data: object) -> dict:
    return {"name": name, "status": PASS if ok else FAIL, "detail": detail, **({"data": data} if data else {})}


def _guard(name: str, fn, *args, **kwargs) -> list[dict]:
    """A check that cannot run is a FAIL, never a crash that hides every other check."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - the probe's job is to report, loudly
        return [_result(name, False, f"check could not run: {type(exc).__name__}: {str(exc)[:160]}")]


def _fresh(name: str, newest: datetime | None, max_age_h: float, now: datetime, what: str) -> dict:
    age = _age_h(newest, now)
    ok = age is not None and age <= max_age_h
    newest_txt = newest.isoformat(timespec="seconds") if newest else "none"
    return _result(name, ok, f"{what}: newest {newest_txt} (age {age}h, max {max_age_h}h)",
                   newest=newest_txt, age_h=age, max_age_h=max_age_h)


# --------------------------------------------------------------------------- 1. ingest freshness

def check_ingest(db: Path, transcripts_db: Path, capture_db: Path, now: datetime, cfg: dict) -> list[dict]:
    out: list[dict] = []
    # claims/verbatim_memories have no created_at/timestamp index and ~10M rows: ids grow with time, so the
    # newest rows by primary key bound the newest timestamp without a full scan.
    with contextlib.closing(ro_connect(db)) as conn:
        row = conn.execute("SELECT MAX(timestamp) FROM (SELECT timestamp FROM verbatim_memories ORDER BY id DESC LIMIT 200)").fetchone()
        out.append(_fresh("ingest.verbatim", parse_ts(row[0]), cfg["verbatim_max_age_h"], now, "verbatim_memories"))
        for family, spec in cfg["claim_families"].items():
            newest = None
            for agent in spec["agents"]:  # per agent: idx_claims_source_agent serves WHERE + ORDER BY id
                got = conn.execute(
                    "SELECT MAX(created_at) FROM (SELECT created_at FROM claims WHERE source_agent=? ORDER BY id DESC LIMIT 20)",
                    (agent,)).fetchone()[0]
                ts = parse_ts(got)
                if ts and (newest is None or ts > newest):
                    newest = ts
            out.append(_fresh(f"ingest.claims.{family}", newest, spec["max_age_h"], now, f"claims from {family} sources"))
        emb = conn.execute("SELECT MAX(updated_at) FROM claim_embeddings").fetchone()[0]
        out.append(_fresh("ingest.embeddings", parse_ts(emb), cfg["embeddings_max_age_h"], now, "claim_embeddings"))
        active, embedded = conn.execute(
            "SELECT COUNT(*), SUM(EXISTS(SELECT 1 FROM claim_embeddings e WHERE e.claim_id=c.id)) "
            "FROM claims c WHERE c.status IN ('confirmed','candidate')").fetchone()
        coverage = (embedded or 0) / active if active else 0.0
        out.append(_result("ingest.embedding_coverage", coverage >= cfg["embeddings_min_coverage"],
                           f"{embedded or 0}/{active} active claims embedded ({coverage:.0%}, min {cfg['embeddings_min_coverage']:.0%})",
                           active=active, embedded=embedded or 0, coverage=round(coverage, 3)))
    out.extend(_guard("ingest.transcripts", _check_transcripts, transcripts_db, now, cfg))
    out.extend(_guard("ingest.capture_queue", _check_capture_queue, capture_db, now, cfg))
    return out


def _check_transcripts(path: Path, now: datetime, cfg: dict) -> list[dict]:
    with contextlib.closing(ro_connect(path)) as conn:
        mtime_ns = conn.execute("SELECT MAX(mtime_ns) FROM files").fetchone()[0]
    newest = datetime.fromtimestamp(mtime_ns / 1e9, timezone.utc) if mtime_ns else None
    return [_fresh("ingest.transcripts", newest, cfg["transcripts_max_age_h"], now, "transcripts.db files.mtime_ns")]


def _check_capture_queue(path: Path, now: datetime, cfg: dict) -> list[dict]:
    with contextlib.closing(ro_connect(path)) as conn:
        count, oldest = conn.execute("SELECT COUNT(*), MIN(captured_at) FROM dream_captures WHERE state='captured'").fetchone()
    age = _age_h(parse_ts(oldest), now)
    ok = count <= cfg["capture_backlog_max"] and (age is None or age <= cfg["capture_backlog_max_age_h"])
    return [_result("ingest.capture_queue", ok,
                    f"{count} captures waiting (max {cfg['capture_backlog_max']}), oldest {age}h (max {cfg['capture_backlog_max_age_h']}h)",
                    waiting=count, oldest_age_h=age)]


# --------------------------------------------------------------------------- 2+3. canary recall and ranking

class Hit:
    """One returned claim, reduced to what the checks read."""

    def __init__(self, claim_id, human_id, text, tier="", status="", freshness=0.0):
        self.claim_id, self.human_id, self.text = claim_id, human_id or "", text or ""
        self.tier, self.status, self.freshness = tier, status, freshness


def canary_hit(canary: dict, hits: list[Hit]) -> bool:
    ids = set(canary.get("expect_human_ids") or [])
    needles = [s.lower() for s in canary.get("expect_substrings") or []]
    for hit in hits[:5]:
        if hit.human_id.split("~")[0] in ids or hit.human_id in ids:
            return True
        if any(n in hit.text.lower() for n in needles):
            return True
    return False


@contextlib.contextmanager
def isolated_recall_env(root: Path):
    """Redirect every file a recall writes (access spool, seen-ids, metrics) to a throwaway dir."""
    overrides = {
        "MEMORYMASTER_SPOOL_DIR": str(root / "spool"),
        "MEMORYMASTER_RECALL_SEEN_DIR": str(root / "seen"),
        "MEMORYMASTER_RECALL_DENSE_LOG": str(root / "recall-dense.jsonl"),
    }
    saved = {k: os.environ.get(k) for k in overrides}
    os.environ.update(overrides)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def hook_recaller(db: Path, workspace: Path):
    """The prompt hook's in-process recall (``context_hook.recall`` with hook_data), top-5 claims."""
    from memorymaster.recall.context_hook import recall

    def run(canary: dict) -> list[Hit]:
        # fresh session per canary: a reused session id hides claims it already received (session dedup)
        hook_data = {"session_id": f"health-probe-{uuid.uuid4().hex[:12]}", "cwd": str(workspace)}
        _text, ids = recall(canary["query"], db_path=str(db), skip_qdrant=True, return_ids=True, hook_data=hook_data)
        ids = list(ids)[:5]
        if not ids:
            return []
        with contextlib.closing(ro_connect(db)) as conn:
            marks = ",".join("?" * len(ids))
            found = {r[0]: r for r in conn.execute(f"SELECT id, human_id, text, tier, status FROM claims WHERE id IN ({marks})", ids)}
        return [Hit(i, found[i][1], found[i][2], found[i][3], found[i][4]) for i in ids if i in found]

    return run


def mcp_recaller(db: Path, workspace: Path):
    """The MCP ``query_memory`` default path (legacy retrieval, trusted, derived scope allowlist), top-20 rows."""
    from memorymaster.core.service import MemoryService
    from memorymaster.recall.planner import RetrievalRequest
    from memorymaster.surfaces.mcp_server import _effective_scope_allowlist

    svc = MemoryService(db_target=str(db), workspace_root=workspace, read_only=True)
    scopes = tuple(_effective_scope_allowlist("", str(workspace)) or ())

    def run(canary: dict) -> list[Hit]:
        result = svc.retrieve(RetrievalRequest(
            query_text=canary["query"], limit=20, trust_mode="trusted", retrieval_mode="legacy", scope_allowlist=scopes))
        return [Hit(r["claim"].id, r["claim"].human_id, r["claim"].text, r["claim"].tier, r["claim"].status,
                    float(r.get("freshness_score") or 0.0)) for r in result.rows]

    return run


def check_canaries(canaries: list[dict], recallers: dict, cfg: dict) -> tuple[list[dict], list[Hit]]:
    """Hit@5 per recall path; also returns the MCP rows for the ranking check."""
    out: list[dict] = []
    mcp_hits: list[Hit] = []
    for path, factory in recallers.items():
        minimum = cfg["canary_min_hit_rate"][path]
        try:
            run = factory()
        except Exception as exc:  # noqa: BLE001
            out.append(_result(f"recall.{path}", False, f"path could not start: {type(exc).__name__}: {str(exc)[:160]}"))
            continue
        hits_n, misses, errors = 0, [], 0
        for canary in canaries:
            try:
                got = run(canary)
            except Exception:  # noqa: BLE001 - an erroring query is a miss
                errors += 1
                got = []
            if path == "mcp":
                mcp_hits.extend(got)
            if canary_hit(canary, got):
                hits_n += 1
            else:
                misses.append(canary["id"])
        rate = hits_n / len(canaries) if canaries else 0.0
        out.append(_result(f"recall.{path}", rate >= minimum,
                           f"hit@5 {hits_n}/{len(canaries)} ({rate:.0%}, min {minimum:.0%}); missed: {', '.join(misses) or 'none'}"
                           + (f"; {errors} errored" if errors else ""),
                           hit_rate=round(rate, 3), hits=hits_n, total=len(canaries), missed=misses, errors=errors))
    return out, mcp_hits


def check_ranking(mcp_hits: list[Hit], cfg: dict) -> list[dict]:
    core = {h.claim_id: h.freshness for h in mcp_hits if h.tier == "core" and h.status == "confirmed"}
    if not core:
        return [_result("ranking.core_freshness", False,
                        "no core confirmed claim came back from any canary (core not surfacing, or recall path down)")]
    median = statistics.median(core.values())
    return [_result("ranking.core_freshness", median >= cfg["core_freshness_min"],
                    f"median freshness_score of {len(core)} core confirmed claims is {median:.3g} (min {cfg['core_freshness_min']:g})",
                    median=median, sample=len(core))]


# --------------------------------------------------------------------------- 4. retention, 5. services

def check_retention(db: Path) -> list[dict]:
    from memorymaster.govern.verbatim_cleanup import plan_retention

    plan = plan_retention(str(db))
    return [_result("retention.within_bounds", bool(plan["within_bounds"]),
                    f"{plan['candidate_rows']} verbatim rows over the retention bounds (truncated={plan['truncated']}); "
                    "outside bounds the stop hook skips storing",
                    candidate_rows=plan["candidate_rows"], truncated=plan["truncated"], limits=plan["limits"])]


def check_services(cfg: dict) -> list[dict]:
    out = []
    for name, url in cfg["services"].items():
        try:
            with urllib.request.urlopen(url, timeout=cfg["service_timeout_s"]) as resp:  # noqa: S310 - loopback URL from CONFIG
                body = json.loads(resp.read().decode("utf-8") or "{}")
            ok = resp.status == 200 and body.get("ok", True) is not False and body.get("status", "ok") == "ok"
            out.append(_result(f"service.{name}", ok, f"{url} -> HTTP {resp.status}"))
        except Exception as exc:  # noqa: BLE001
            out.append(_result(f"service.{name}", False, f"{url} unreachable: {type(exc).__name__}"))
    return out


# --------------------------------------------------------------------------- orchestration

def run_probe(db: Path, *, workspace: Path, canaries_path: Path, transcripts_db: Path, capture_db: Path,
              now: datetime | None = None, cfg: dict | None = None, recallers: dict | None = None) -> dict:
    cfg = cfg or CONFIG
    now = now or datetime.now(timezone.utc)
    canaries = json.loads(Path(canaries_path).read_text(encoding="utf-8"))["canaries"]
    checks = _guard("ingest", check_ingest, db, transcripts_db, capture_db, now, cfg)
    with tempfile.TemporaryDirectory(prefix="mm-health-") as tmp, isolated_recall_env(Path(tmp)):
        recallers = recallers or {"hook": lambda: hook_recaller(db, workspace), "mcp": lambda: mcp_recaller(db, workspace)}
        canary_checks, mcp_hits = check_canaries(canaries, recallers, cfg)
    checks += canary_checks + check_ranking(mcp_hits, cfg)
    checks += _guard("retention.within_bounds", check_retention, db)
    checks += check_services(cfg)
    failures = [c["name"] for c in checks if c["status"] == FAIL]
    return {
        "schema": 1,
        "generated_at": now.isoformat(timespec="seconds"),
        "db": Path(db).name,
        "status": FAIL if failures else PASS,
        "failures": failures,
        "env": {k: os.environ.get(k, "") for k in ("MEMORYMASTER_RECALL_DENSE", "MEMORYMASTER_RECALL_TRANSCRIPT_FALLBACK", "MEMORYMASTER_WAL_DISCIPLINE")},
        "checks": checks,
    }


def write_report(report: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    day = parse_ts(report["generated_at"]).astimezone(timezone.utc).strftime("%Y-%m-%d")
    dated = out_dir / f"{day}.json"
    for target in (dated, out_dir / "latest.json"):
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, target)
    return dated


# --------------------------------------------------------------------------- alert

def alert_body(report: dict) -> str:
    lines = ["criteria:"]
    for check in report["checks"]:
        if check["status"] == FAIL:
            lines.append(f"  {check['name']}: fail — {check['detail']}")
    passed = sum(1 for c in report["checks"] if c["status"] == PASS)
    lines += [f"  other checks: pass — {passed} of {len(report['checks'])} passed",
              "files_changed: none",
              "next_action: read the latest health report in the MemoryMaster checkpoints folder and fix the failing stores",
              f"voz: {VOZ}"]
    return "\n".join(lines)


def orca_executable() -> str:
    found = shutil.which("orca")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA", "")
    return str(Path(local) / "Programs" / "orca" / "resources" / "bin" / "orca.exe")


def send_orca(run_id: str, subject: str, body: str) -> None:
    done = subprocess.run(
        [orca_executable(), "orchestration", "send", "--to", f"run:{run_id}", "--type", "status",
         "--subject", subject, "--body", body],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    if done.returncode != 0:
        raise RuntimeError(f"orca exit {done.returncode}: {done.stderr.strip()[:200]}")


def maybe_alert(report: dict, out_dir: Path, orchestrators: Path, *, sender=None) -> dict:
    """One alert per UTC day unless the failing set changes. State is written only after a delivered send."""
    failures = sorted(report["failures"])
    if not failures:
        return {"sent": False, "reason": "no failures"}
    day = parse_ts(report["generated_at"]).astimezone(timezone.utc).strftime("%Y-%m-%d")
    state_path = out_dir / "alert-state.json"
    with contextlib.suppress(OSError, ValueError):
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("utc_date") == day and state.get("failures") == failures:
            return {"sent": False, "reason": "already alerted today for this failure set"}
    try:
        run_id = json.loads(Path(orchestrators).read_text(encoding="utf-8"))["fleet"]["run_id"]
        (sender or send_orca)(run_id, f"[MM-SALUD] {len(failures)} fallas", alert_body(report))
    except Exception as exc:  # noqa: BLE001 - a failed alert must not hide the report; retried next run
        return {"sent": False, "reason": f"send failed: {type(exc).__name__}: {str(exc)[:160]}"}
    state_path.write_text(json.dumps({"utc_date": day, "failures": failures}), encoding="utf-8")
    return {"sent": True, "reason": "delivered"}


def main(argv: list[str] | None = None) -> int:
    default_db = os.environ.get("MEMORYMASTER_DEFAULT_DB") or str(REPO / "memorymaster.db")
    home = Path.home() / ".memorymaster"
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", default=default_db)
    parser.add_argument("--workspace", default=str(REPO), help="memorymaster checkout; sets the recall scope")
    parser.add_argument("--canaries", default=str(Path(__file__).with_name("health_canaries.json")))
    parser.add_argument("--transcripts-db", default=str(home / "transcripts.db"))
    parser.add_argument("--capture-db", default=str(home / "capture-control.db"))
    parser.add_argument("--out", default=str(home / "checkpoints" / "health"))
    parser.add_argument("--orchestrators", default=str(REPO.parent / "_intel" / "orchestrators.json"))
    parser.add_argument("--now", help="ISO timestamp override (tests)")
    parser.add_argument("--no-alert", action="store_true")
    args = parser.parse_args(argv)
    logging.disable(logging.INFO)  # recall logs one INFO line per FTS query; the report is the output

    report = run_probe(Path(args.db), workspace=Path(args.workspace).resolve(), canaries_path=Path(args.canaries),
                       transcripts_db=Path(args.transcripts_db), capture_db=Path(args.capture_db),
                       now=parse_ts(args.now) if args.now else None)
    out_dir = Path(args.out)
    report["alert"] = ({"sent": False, "reason": "--no-alert"} if args.no_alert
                       else maybe_alert(report, out_dir, Path(args.orchestrators)))
    write_report(report, out_dir)
    for check in report["checks"]:
        print(f"{check['status']:4} {check['name']}: {check['detail']}")
    print(f"{report['status']}: {len(report['failures'])} failing; alert: {report['alert']['reason']}")
    return 1 if report["failures"] else 0


if __name__ == "__main__":
    sys.exit(main())

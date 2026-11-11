#!/usr/bin/env python3
"""Daily read-only health probe for MemoryMaster (T-2121).

Fails loudly when the *user outcome* breaks, not a proxy: stores stopped ingesting, recall returns
off-topic rows, core claims rank at the floor, the verbatim stop-hook skips storing, a service is down.
On 2026-11-03 all of those had been true for months while tests, latency and citation rate stayed green.

Read-only by construction: the authoritative DB is opened ``mode=ro`` + ``query_only``; recall runs with a
read-only ``MemoryService`` and its access spool, session-seen and metrics files are redirected to a
throwaway directory, so a canary query can never raise a claim's ``access_count`` (self-reinforcement).

Report: ``<out>/<UTC date>.json`` and ``<out>/latest.json``. Exit 1 on any FAIL. One Orca message goes to the
Fleet run when the state CHANGES: a check turns red, a red one gets materially worse, one recovers, or (at most
once per ``alert_reminder_days``) a check stays red (``--no-alert`` off).

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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:  # `python scripts/x.py` puts scripts/, not the repo, on sys.path
    sys.path.insert(0, str(REPO))

from scripts.check_hook_template_drift import TEMPLATE_DIR, compare_hook  # noqa: E402

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
    # The live dense index is the embeddinggemma service (claim_embeddings is only a cache of the opt-in hybrid path).
    "verbatim_skip_window_h": 24,  # a skip recorded by the stop hook inside this window is a FAIL
    "dense_url": "http://127.0.0.1:8767/health",
    "dense_min_indexed_ratio": 0.95,  # indexed / confirmed claims
    "dense_max_sync_age_s": 600,  # the service syncs every 60 s
    "capture_backlog_max": 1000,  # dream_captures still in state 'captured'
    "capture_backlog_max_age_h": 168,  # age of the oldest one
    "canary_min_hit_rate": {"hook": 0.6, "mcp": 0.6},  # hit@5 per recall path
    "core_freshness_min": 1e-3,  # median freshness_score of core confirmed claims the canaries return
    "services": {"mcp_http": "http://127.0.0.1:8766/healthz"},
    "service_timeout_s": 4.0,
    "dream_runs_window_h": 48,  # red when more than half of the finished dreaming runs in this window failed
    "dream_runs_min_sample": 4,  # fewer runs than this in the window: judge only the newest run
    "dream_schedule_h": 1,  # the beat ticks every 5 min; an unhealthy newest run older than this means nothing ran since
    "dream_drain_window_h": 12,  # red when captures wait and none reached applied/skipped/quarantined in this window
    "steward_max_age_h": 12,  # the steward is scheduled every 6 h: red after two missed cycles
    "alert_reminder_days": 7,  # a check that stays red is re-announced at most this often
    "alert_worse_factor": 1.5,  # a red check is "materially worse" at this multiple of its level at the last alert
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
    """Each store is guarded on its own: one that cannot be read is a FAIL and the others still report."""
    out = _guard("ingest.verbatim", _check_verbatim, db, now, cfg)
    for family, spec in cfg["claim_families"].items():
        out += _guard(f"ingest.claims.{family}", _check_claim_family, db, family, spec, now)
    out += _guard("ingest.transcripts", _check_transcripts, transcripts_db, now, cfg)
    out += _guard("ingest.capture_queue", _check_capture_queue, capture_db, now, cfg)
    return out


# claims/verbatim_memories have no created_at/timestamp index and ~10M rows: ids grow with time, so the
# newest rows by primary key bound the newest timestamp without a full scan.
def _check_verbatim(db: Path, now: datetime, cfg: dict) -> list[dict]:
    with contextlib.closing(ro_connect(db)) as conn:
        row = conn.execute("SELECT MAX(timestamp) FROM (SELECT timestamp FROM verbatim_memories ORDER BY id DESC LIMIT 200)").fetchone()
    return [_fresh("ingest.verbatim", parse_ts(row[0]), cfg["verbatim_max_age_h"], now, "verbatim_memories")]


def _check_claim_family(db: Path, family: str, spec: dict, now: datetime) -> list[dict]:
    newest = None
    with contextlib.closing(ro_connect(db)) as conn:
        for agent in spec["agents"]:  # per agent: idx_claims_source_agent serves WHERE + ORDER BY id
            got = conn.execute(
                "SELECT MAX(created_at) FROM (SELECT created_at FROM claims WHERE source_agent=? ORDER BY id DESC LIMIT 20)",
                (agent,)).fetchone()[0]
            ts = parse_ts(got)
            if ts and (newest is None or ts > newest):
                newest = ts
    return [_fresh(f"ingest.claims.{family}", newest, spec["max_age_h"], now, f"claims from {family} sources")]


def _check_transcripts(path: Path, now: datetime, cfg: dict) -> list[dict]:
    with contextlib.closing(ro_connect(path)) as conn:
        mtime_ns = conn.execute("SELECT MAX(mtime_ns) FROM files").fetchone()[0]
    newest = datetime.fromtimestamp(mtime_ns / 1e9, timezone.utc) if mtime_ns else None
    return [_fresh("ingest.transcripts", newest, cfg["transcripts_max_age_h"], now, "transcripts.db files.mtime_ns")]


def _check_capture_queue(path: Path, now: datetime, cfg: dict) -> list[dict]:
    with contextlib.closing(ro_connect(path)) as conn:
        # Same definition as ledger.eligible() and operational_review: below two turns a capture is never scheduled
        # (and never retired), so counting it would raise a permanent alarm no drain can clear (T-2123).
        count, oldest = conn.execute(
            "SELECT COUNT(*), MIN(last_activity_at) FROM dream_captures "
            "WHERE state IN ('captured','retryable') AND turn_count >= 2").fetchone()
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

def check_retention(db: Path, now: datetime) -> list[dict]:
    """The stop hook's own O(1) staleness test: is the oldest dated verbatim row past the archive window?"""
    from memorymaster.govern.verbatim_cleanup import retention_due

    due = retention_due(str(db), now=now)
    return [_result("retention.due", not due["due"],
                    f"oldest dated verbatim row is {due['oldest_age_days']} days old; "
                    + ("archive retention is overdue" if due["due"] else "within the archive window"),
                    oldest_at=due["oldest_at"], oldest_age_days=due["oldest_age_days"])]


def check_verbatim_marker(path: Path, now: datetime, cfg: dict) -> list[dict]:
    """What the Stop hook says about itself (verbatim-health.json, T-2116): overdue retention, a failed check or a recent skip."""
    if not path.is_file():
        return [_result("verbatim.marker", True, "no marker yet (stop hook not reinstalled, or no Stop since); ingest.verbatim covers the outage")]
    marker = json.loads(path.read_text(encoding="utf-8"))
    problems = []
    if marker.get("retention_due"):
        problems.append("retention_due")
    if marker.get("retention_check_error"):
        problems.append(f"retention check failed ({marker['retention_check_error']})")
    skipped_at = parse_ts(marker.get("last_skip_at"))
    skip_age = _age_h(skipped_at, now)
    if skip_age is not None and skip_age <= cfg["verbatim_skip_window_h"]:
        problems.append(f"store skipped {skip_age}h ago ({marker.get('last_skip_reason') or 'no reason recorded'})")
    return [_result("verbatim.marker", not problems,
                    "; ".join(problems) or f"healthy (last store {marker.get('last_store_at') or 'unknown'})",
                    last_store_at=marker.get("last_store_at"), store_skipped_count=marker.get("store_skipped_count"))]


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


def check_dense(db: Path, now: datetime, cfg: dict) -> list[dict]:
    """The dense recall service: index size vs confirmed claims, last sync age, sync errors."""
    try:
        with urllib.request.urlopen(cfg["dense_url"], timeout=cfg["service_timeout_s"]) as resp:  # noqa: S310 - loopback URL from CONFIG
            body = json.loads(resp.read().decode("utf-8") or "{}")
        if resp.status != 200 or body.get("ok") is False:
            raise RuntimeError(f"HTTP {resp.status}, ok={body.get('ok')}")
    except Exception as exc:  # noqa: BLE001
        return [_result("dense.service", False, f"dense service did not answer at {cfg['dense_url']}: {type(exc).__name__}: {str(exc)[:100]}")]
    with contextlib.closing(ro_connect(db)) as conn:
        confirmed = conn.execute("SELECT COUNT(*) FROM claims WHERE status='confirmed'").fetchone()[0]
    indexed = int(body.get("indexed") or 0)
    ratio = indexed / confirmed if confirmed else 0.0
    last_sync = body.get("last_sync")
    age = None if last_sync is None else round(now.timestamp() - float(last_sync))
    errors = int(body.get("sync_errors") or 0)
    return [
        _result("dense.indexed", ratio >= cfg["dense_min_indexed_ratio"],
                f"{indexed}/{confirmed} confirmed claims indexed ({ratio:.0%}, min {cfg['dense_min_indexed_ratio']:.0%})",
                indexed=indexed, confirmed=confirmed, ratio=round(ratio, 3)),
        _result("dense.sync_fresh", age is not None and age <= cfg["dense_max_sync_age_s"],
                f"last index sync {age if age is not None else 'never'}s ago (max {cfg['dense_max_sync_age_s']}s)", age_s=age),
        _result("dense.sync_errors", errors == 0, f"{errors} sync errors since the service started", sync_errors=errors),
    ]


# --------------------------------------------------------------------------- 6. hooks, dreaming runs, steward

def hook_dirs_default() -> dict[str, Path]:
    home = Path.home()
    return {"claude": home / ".claude" / "hooks", "codex": home / ".codex" / "hooks",
            "agy": home / ".gemini" / "config", "opencode": home / ".config" / "opencode"}


def _hook_candidates(dirs: dict[str, Path]):
    """(label, templates, installed path): every hook an installer can put in each directory.

    Several templates for one file means the installer picks one (the OpenCode plugin has two generations)."""
    templates = Path(TEMPLATE_DIR)
    for cli in ("claude", "codex"):
        if cli in dirs:
            for template in sorted(templates.glob("*.py")):
                yield f"{cli}/{template.name}", [template], Path(dirs[cli]) / template.name
    if "agy" in dirs:
        yield "agy/memorymaster-agy.py", [templates / "memorymaster-agy.py"], Path(dirs["agy"]) / "memorymaster-agy.py"
    if "opencode" in dirs:
        opencode, base = templates.parent / "opencode", Path(dirs["opencode"])
        yield "opencode/runtime.mjs", [opencode / "runtime.mjs"], base / "memorymaster" / "runtime.mjs"
        yield ("opencode/memorymaster.js", [opencode / "plugin-v2.mjs", opencode / "plugin-v1.mjs"],
               base / "plugins" / "memorymaster.js")


def read_hook_allowlist(path: Path | None) -> set[str]:
    """Operator-owned list of deliberately customised hooks: one file name (or ``cli/name``) per line, ``#`` comments."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines() if path else []
    except OSError:  # missing file = empty list
        return set()
    return {line.split("#", 1)[0].strip() for line in lines if line.split("#", 1)[0].strip()}


def check_hook_drift(dirs: dict[str, Path], allowlist_path: Path | None) -> list[dict]:
    """Installed hooks must equal their template, placeholders and line endings aside (a stale hook ran for weeks unseen)."""
    allowed = read_hook_allowlist(allowlist_path)
    drifted, customised, same, absent = [], [], 0, 0
    for label, templates, installed in _hook_candidates(dirs):
        if not installed.is_file():
            absent += 1  # a template with no installed copy is a note, not a failure
            continue
        text = installed.read_text(encoding="utf-8", errors="replace")
        deltas = [compare_hook(t.read_text(encoding="utf-8"), text) for t in templates]
        if None in deltas:
            same += 1
            continue
        entry = f"{label} (+{deltas[0][0]}/-{deltas[0][1]})"
        (customised if label in allowed or Path(label).name in allowed else drifted).append(entry)
    detail = f"{same} installed hooks match their template; {absent} templates not installed (note)"
    if customised:
        detail += f"; customised by the operator: {', '.join(customised)}"
    if drifted:
        detail = f"installed hooks differ from the templates: {', '.join(drifted)}; " + detail
    return [_result("hooks.template_drift", not drifted, detail,
                    drifted=len(drifted), customised=len(customised), matching=same, not_installed=absent)]


def check_dream_runs(capture_db: Path, now: datetime, cfg: dict) -> list[dict]:
    """Finished (non-dry) dreaming runs in the window; ``failed`` and ``abandoned`` (killed by the scheduler's time
    limit) both count as not ok. With enough runs, red above half; with fewer, red when the newest one is not ok
    and older than the schedule."""
    cutoff = now.timestamp() - cfg["dream_runs_window_h"] * 3600
    with contextlib.closing(ro_connect(capture_db)) as conn:
        rows = conn.execute("SELECT status, started_at FROM dream_runs WHERE dry_run=0 ORDER BY started_at DESC LIMIT 500").fetchall()
    finished = [(status, parse_ts(started) or now) for status, started in rows
                if status != "running" and (parse_ts(started) or now).timestamp() >= cutoff]
    statuses = [status for status, _ in finished]
    failed, abandoned = statuses.count("failed"), statuses.count("abandoned")
    bad = failed + abandoned
    if len(finished) >= cfg["dream_runs_min_sample"]:
        ok, rule = bad * 2 <= len(finished), "red above half"
    else:
        newest_status, newest_at = finished[0] if finished else ("ok", now)
        ok = newest_status not in ("failed", "abandoned") or (now - newest_at).total_seconds() <= cfg["dream_schedule_h"] * 3600
        rule = f"under {cfg['dream_runs_min_sample']} runs: red when the newest is not ok and older than {cfg['dream_schedule_h']}h"
    return [_result("dreaming.runs", ok,
                    f"{failed} failed and {abandoned} abandoned of {len(finished)} dreaming runs in the last {cfg['dream_runs_window_h']}h ({rule})",
                    failed=failed, abandoned=abandoned, total=len(finished),
                    failed_share=round(bad / len(finished), 2) if finished else 0.0)]


def check_dream_drain(capture_db: Path, now: datetime, cfg: dict) -> list[dict]:
    """Throughput, not backlog size (``ingest.capture_queue`` owns that): red when work is waiting and no capture
    reached a terminal state (applied / skipped / quarantined) in the window."""
    since = (now - timedelta(hours=cfg["dream_drain_window_h"])).isoformat()
    with contextlib.closing(ro_connect(capture_db)) as conn:
        waiting = conn.execute("SELECT COUNT(*) FROM dream_captures WHERE state IN ('captured','retryable') AND turn_count >= 2").fetchone()[0]
        exited = conn.execute("SELECT COUNT(*) FROM dream_captures WHERE state IN ('applied','skipped','quarantined') "
                              "AND turn_count >= 2 AND updated_at >= ?", (since,)).fetchone()[0]
    return [_result("dreaming.drain", not (waiting and not exited),
                    f"{exited} captures reached a terminal state in the last {cfg['dream_drain_window_h']}h with {waiting} waiting",
                    exited=exited, waiting_now=waiting)]


def check_steward_recency(log: Path, now: datetime, cfg: dict) -> list[dict]:
    """Newest completed steward cycle: the scheduled-task wrapper logs one ``job_finish`` per cycle (surfaces/scheduled_task.py)."""
    newest = None
    try:
        with Path(log).open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if '"job_finish"' not in line or '"steward"' not in line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                stamp = parse_ts(event.get("ts")) if isinstance(event, dict) else None
                if (stamp and event.get("event") == "job_finish" and event.get("job") == "steward"
                        and not event.get("exit_code") and event.get("outcome") not in ("failed", "error")
                        and (newest is None or stamp > newest)):
                    newest = stamp
    except OSError:
        pass  # no log: no completed cycle on record, reported by _fresh as "none"
    return [_fresh("steward.recency", newest, cfg["steward_max_age_h"], now, "last completed steward cycle")]


# --------------------------------------------------------------------------- orchestration

def run_probe(db: Path, *, workspace: Path, canaries_path: Path, transcripts_db: Path, capture_db: Path,
              verbatim_health: Path | None = None, now: datetime | None = None, cfg: dict | None = None,
              recallers: dict | None = None, hook_dirs: dict[str, Path] | None = None,
              hook_allowlist: Path | None = None, steward_log: Path | None = None) -> dict:
    cfg = cfg or CONFIG
    now = now or datetime.now(timezone.utc)
    canaries = json.loads(Path(canaries_path).read_text(encoding="utf-8"))["canaries"]
    checks = _guard("ingest", check_ingest, db, transcripts_db, capture_db, now, cfg)
    checks += _guard("dreaming.runs", check_dream_runs, capture_db, now, cfg)
    checks += _guard("dreaming.drain", check_dream_drain, capture_db, now, cfg)
    if steward_log is not None:
        checks += _guard("steward.recency", check_steward_recency, steward_log, now, cfg)
    if hook_dirs is not None:  # opt-in: a caller that passes no directories never reads the machine's real hooks
        checks += _guard("hooks.template_drift", check_hook_drift, hook_dirs, hook_allowlist)
    with tempfile.TemporaryDirectory(prefix="mm-health-") as tmp, isolated_recall_env(Path(tmp)):
        recallers = recallers or {"hook": lambda: hook_recaller(db, workspace), "mcp": lambda: mcp_recaller(db, workspace)}
        canary_checks, mcp_hits = check_canaries(canaries, recallers, cfg)
    checks += canary_checks + check_ranking(mcp_hits, cfg)
    checks += _guard("retention.due", check_retention, db, now)
    if verbatim_health is not None:
        checks += _guard("verbatim.marker", check_verbatim_marker, verbatim_health, now, cfg)
    checks += check_services(cfg)
    checks += _guard("dense.service", check_dense, db, now, cfg)
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

RECOVERED_VOZ = "Terminó la sonda diaria de salud y las fallas que había avisado ya se resolvieron."


def alert_body(report: dict, tags: dict[str, str] | None = None, recovered: tuple[str, ...] = ()) -> str:
    """``tags`` annotates a failing check (new / worse / reminder); ``recovered`` names checks that turned green."""
    tags = tags or {}
    lines = ["criteria:"]
    for check in report["checks"]:
        if check["status"] == FAIL:
            note = f" [{tags[check['name']]}]" if check["name"] in tags else ""
            lines.append(f"  {check['name']}: fail — {check['detail']}{note}")
    lines += [f"  {name}: pass — back to green" for name in recovered]
    passed = sum(1 for c in report["checks"] if c["status"] == PASS)
    healthy = not report["failures"]
    lines += [f"  other checks: pass — {passed} of {len(report['checks'])} passed",
              "files_changed: none",
              "next_action: " + ("none, every check is green again" if healthy else
                                 "read the latest health report in the MemoryMaster checkpoints folder and fix the failing stores"),
              f"voz: {RECOVERED_VOZ if healthy else VOZ}"]
    return "\n".join(lines)


def orca_executable() -> str:
    found = shutil.which("orca")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA", "")
    return str(Path(local) / "Programs" / "orca" / "resources" / "bin" / "orca.exe")


SENDER_LABEL = "memorymaster-health-probe"
SENDER_ENV = "MEMORYMASTER_HEALTH_PROBE_SENDER"


def sender_label() -> str:
    """Orca accepts a plain label as `--from` with no terminal behind it (wezbridge sender-identity.cjs, T-1219).

    Never a lane handle and never ORCA_TERMINAL_HANDLE: a task launched from a lane pane would inherit it and sign as that lane."""
    return os.environ.get(SENDER_ENV, "").strip() or SENDER_LABEL


def send_orca(run_id: str, subject: str, body: str) -> None:
    env = {k: v for k, v in os.environ.items() if k != "ORCA_TERMINAL_HANDLE"}
    done = subprocess.run(
        [orca_executable(), "orchestration", "send", "--from", sender_label(), "--to", f"run:{run_id}", "--type", "status",
         "--subject", subject, "--body", body],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, env=env)
    if done.returncode != 0:
        why = (done.stderr.strip() or done.stdout.strip())[:200]  # Orca reports its refusal code on either stream
        raise RuntimeError(f"orca exit {done.returncode}: {why}")


# Severity of a red check, for "materially worse": the first of these numbers a check reports.
SEVERITY_KEYS = ("waiting", "waiting_now", "drifted", "failed_share", "age_h")


def _severity(check: dict | None) -> float | None:
    data = (check or {}).get("data") or {}
    for key in SEVERITY_KEYS:
        value = data.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _load_alert_state(path: Path) -> dict[str, dict]:
    """name -> {"on": date of the last alert naming it, "severity": its level then}. Reads the old
    ``{"utc_date", "failures"}`` file too (its dates carry over, its severities are unknown)."""
    with contextlib.suppress(OSError, ValueError, AttributeError, TypeError):
        state = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(state.get("checks"), dict):
            return {name: dict(entry) for name, entry in state["checks"].items() if isinstance(entry, dict)}
        return {name: {"on": state.get("utc_date"), "severity": None} for name in state.get("failures", [])}
    return {}


def _days_since(date: object, day: str) -> float:
    try:
        return (datetime.strptime(day, "%Y-%m-%d") - datetime.strptime(str(date), "%Y-%m-%d")).days
    except ValueError:
        return float("inf")  # an unreadable date counts as long ago: one reminder, then the state is rewritten


def maybe_alert(report: dict, out_dir: Path, orchestrators: Path, *, sender=None, cfg: dict | None = None) -> dict:
    """Alert on a state CHANGE, not on a schedule: a check newly red, a red check materially worse than at its
    last alert, a check recovered, or a reminder for one that stayed red for ``alert_reminder_days``. State is
    written only after a delivered send."""
    cfg = cfg or CONFIG
    failures = sorted(report["failures"])
    day = parse_ts(report["generated_at"]).astimezone(timezone.utc).strftime("%Y-%m-%d")
    state_path = out_dir / "alert-state.json"
    previous = _load_alert_state(state_path)
    severity = {c["name"]: _severity(c) for c in report["checks"]}
    tags: dict[str, str] = {}
    for name in failures:
        before = previous.get(name)
        if before is None:
            tags[name] = "new"
            continue
        old, now_level = before.get("severity"), severity.get(name)
        if old is not None and now_level is not None and now_level > old and now_level >= old * cfg["alert_worse_factor"]:
            tags[name] = "worse than at the last alert"
        elif _days_since(before.get("on"), day) >= cfg["alert_reminder_days"]:
            tags[name] = f"reminder, still failing since {before.get('on')}"
    recovered = tuple(sorted(name for name in previous if name not in failures))
    if not tags and not recovered:
        return {"sent": False, "reason": "no change since the last alert" if failures else "no failures"}
    try:
        run_id = json.loads(Path(orchestrators).read_text(encoding="utf-8"))["fleet"]["run_id"]
        subject = f"[MM-SALUD] {len(failures)} fallas" if failures else "[MM-SALUD] recuperado"
        (sender or send_orca)(run_id, subject, alert_body(report, tags, recovered))
    except Exception as exc:  # noqa: BLE001 - a failed alert must not hide the report; retried next run
        return {"sent": False, "reason": f"send failed: {type(exc).__name__}: {str(exc)[:160]}"}
    state_path.write_text(json.dumps({"utc_date": day, "failures": failures,
                                      "checks": {n: {"on": day, "severity": severity.get(n)} for n in failures}}),
                          encoding="utf-8")
    return {"sent": True, "reason": "delivered"}


@contextlib.contextmanager
def quiet_info_logs():
    """Recall logs one INFO line per FTS query; the report is the output. Process-global, so always restored."""
    previous = logging.root.manager.disable
    logging.disable(logging.INFO)
    try:
        yield
    finally:
        logging.disable(previous)


def main(argv: list[str] | None = None) -> int:
    default_db = os.environ.get("MEMORYMASTER_DEFAULT_DB") or str(REPO / "memorymaster.db")
    home = Path.home() / ".memorymaster"
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", default=default_db)
    parser.add_argument("--workspace", default=str(REPO), help="memorymaster checkout; sets the recall scope")
    parser.add_argument("--canaries", default=str(Path(__file__).with_name("health_canaries.json")))
    parser.add_argument("--transcripts-db", default=str(home / "transcripts.db"))
    parser.add_argument("--capture-db", default=str(home / "capture-control.db"))
    parser.add_argument("--verbatim-health", default=str(home / "hook_state" / "verbatim-health.json"))
    parser.add_argument("--out", default=str(home / "checkpoints" / "health"))
    parser.add_argument("--steward-log", default=str(home / "logs" / "steward.log"))
    parser.add_argument("--hook-allowlist", default=str(home / "hook-drift-allowlist.txt"),
                        help="operator-owned: one deliberately customised hook file name per line")
    parser.add_argument("--orchestrators", default=str(REPO.parent / "_intel" / "orchestrators.json"))
    parser.add_argument("--now", help="ISO timestamp override (tests)")
    parser.add_argument("--no-alert", action="store_true")
    args = parser.parse_args(argv)

    with quiet_info_logs():
        report = run_probe(Path(args.db), workspace=Path(args.workspace).resolve(), canaries_path=Path(args.canaries),
                           transcripts_db=Path(args.transcripts_db), capture_db=Path(args.capture_db),
                           verbatim_health=Path(args.verbatim_health), now=parse_ts(args.now) if args.now else None,
                           hook_dirs=hook_dirs_default(), hook_allowlist=Path(args.hook_allowlist),
                           steward_log=Path(args.steward_log))
    out_dir = Path(args.out)
    report["alert"] = {"sent": False, "reason": "--no-alert" if args.no_alert else "pending"}
    write_report(report, out_dir)  # first: it creates out_dir, and the report must exist even if the alert fails
    if not args.no_alert:
        try:
            report["alert"] = maybe_alert(report, out_dir, Path(args.orchestrators))
        except Exception as exc:  # noqa: BLE001 - a failed alert never masks the exit code
            report["alert"] = {"sent": False, "reason": f"alert error: {type(exc).__name__}: {str(exc)[:160]}"}
        write_report(report, out_dir)
    for check in report["checks"]:
        print(f"{check['status']:4} {check['name']}: {check['detail']}")
    print(f"{report['status']}: {len(report['failures'])} failing; alert: {report['alert']['reason']}")
    return 1 if report["failures"] else 0


if __name__ == "__main__":
    sys.exit(main())

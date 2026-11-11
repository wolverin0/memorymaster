#!/usr/bin/env python3
"""Read-only daily delivery metrics (T-2272): the ROADMAP "Daily metrics" table as JSON.

Measures, over a trailing window (default 7 days):
  a. recall returns (usage_feedback rows) split into transport_notice, internal_process (both = machine_share)
     and task_brief (legitimate demand);
  b. injected claims cited, via scripts/review_recall_citations.py (diagnostic only);
  c. dreaming queue by status and p95 hours from capture to applied (capture-control ledger);
  d. injected tokens per turn and hook latency, from recall-dense.jsonl (null with a reason if absent).

Every database is opened ``?mode=ro`` (``PRAGMA query_only=ON``); nothing is written except the
optional ``--out`` report. Prefixes of user queries pass through ``redact_text`` before emission.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import sys
import time
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for _path in (REPO_ROOT, REPO_ROOT / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import review_recall_citations  # noqa: E402  (sibling script; reused for the citation scan)
from memorymaster.core.capture_control import capture_state_path  # noqa: E402
from memorymaster.core.security import redact_text  # noqa: E402
from memorymaster.recall.dense_recall import read_events  # noqa: E402
from memorymaster.recall.machine_traffic import INTERNAL_PROCESS_PATTERNS, TRANSPORT_NOTICE_PATTERNS  # noqa: E402

# Matched against the left-stripped start of usage_feedback.query_text. Widening a list changes the
# headline share: do it deliberately, the unmatched-prefix list is there to guide it.
# transport_notice: TRANSPORT_NOTICE_PATTERNS, the one list shared with the hooks (recall/machine_traffic.py).
# internal_process: our own LLM pipelines prompting a CLI (not human demand): INTERNAL_PROCESS_PATTERNS, the same list
# as the hooks (it includes the external workflow/fleet templates).
# task_brief: an Orca coordinator dispatching real work. Legitimate demand: counted apart, NOT machine.
TASK_BRIEF_PATTERNS = tuple(re.compile(p) for p in (
    r"You are working inside Orca",
    r"Please carry out this task from my Orca coordinator",
    r"Revisa los lanes de FuturaMAX",
    r"You are a subagent spawned by",
    r"Ten[eé]s trabajo nuevo del orquestador",
    r"Trabajo nuevo del orquestador",
))
CATEGORIES = {  # checked in order; the first match wins
    "transport_notice": TRANSPORT_NOTICE_PATTERNS,
    "internal_process": INTERNAL_PROCESS_PATTERNS,
    "task_brief": TASK_BRIEF_PATTERNS,
}
MACHINE_CATEGORIES = ("transport_notice", "internal_process")  # machine_share = these two
HEAD_CHARS = 96            # SQL-side head kept per query; every pattern anchors well inside it
PREFIX_CHARS = 32          # unmatched prefix width reported
TOP_UNMATCHED = 20
AUTOMATED_REPEAT = 100     # an unmatched prefix returned this often is probably templated
CHARS_PER_TOKEN = 4        # estimate, same convention as recall/context_hook.py
LEFT_STRIP = "char(32,9,10,13)"


def classify_query(text: str) -> str | None:
    """Category of a recall query (see CATEGORIES), None when no pattern matches."""
    head = (text or "").lstrip()
    for name, patterns in CATEGORIES.items():
        if any(p.match(head) for p in patterns):
            return name
    return None


def percentile(values, q: float):
    """Nearest-rank percentile (same as measure_consolidate_latency); None when empty."""
    data = sorted(values)
    return data[max(math.ceil(len(data) * q), 1) - 1] if data else None


def open_ro(path) -> sqlite3.Connection:
    """Read-only handle; a missing file raises instead of being created."""
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=ON")
    return conn


def parse_ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _unavailable(reason: str) -> dict:
    return {"value": None, "reason": reason}


def machine_notice_share(conn: sqlite3.Connection, since: datetime) -> dict:
    rows = conn.execute(
        f"""SELECT substr(ltrim(query_text, {LEFT_STRIP}), 1, {HEAD_CHARS}) AS head,
                   COUNT(*), COUNT(DISTINCT timestamp || query_text)
            FROM usage_feedback WHERE timestamp >= ? AND was_returned = 1 GROUP BY head""",
        (since.isoformat(),),
    ).fetchall()
    total = events = 0
    by_returns: Counter = Counter()
    by_events: Counter = Counter()
    unmatched: Counter = Counter()
    for head, returns, distinct_events in rows:
        total += returns
        events += distinct_events
        category = classify_query(head)
        if category:
            by_returns[category] += returns
            by_events[category] += distinct_events
        else:
            unmatched[head[:PREFIX_CHARS]] += returns

    def share(count: int, whole: int):
        return round(count / whole, 4) if whole else None

    machine = sum(by_returns[c] for c in MACHINE_CATEGORIES)
    machine_events = sum(by_events[c] for c in MACHINE_CATEGORIES)
    top = []
    for prefix, count in unmatched.most_common(TOP_UNMATCHED):
        top.append({"prefix": redact_text(prefix)[0], "returns": count,
                    "looks_automated": prefix[:1] in "<[" or count >= AUTOMATED_REPEAT})
    return {
        "source": "memorymaster.db usage_feedback (was_returned=1)",
        "headline": "machine_share",
        "machine_share": share(machine, total),
        "machine_share_of_events": share(machine_events, events),
        "machine_definition": "+".join(MACHINE_CATEGORIES) + "; task_brief is legitimate demand and excluded",
        "recall_returns": total,
        "recall_events": events,
        "categories": {
            name: {"returns": by_returns[name], "share_of_returns": share(by_returns[name], total),
                   "events": by_events[name], "share_of_events": share(by_events[name], events),
                   "patterns": [p.pattern for p in patterns]}
            for name, patterns in CATEGORIES.items()
        },
        "unmatched_returns": sum(unmatched.values()),
        "top_unmatched_prefixes": top,
    }


def citation_rate(home: Path, days: int, now: datetime) -> dict:
    diagnostics: Counter = Counter()
    result = review_recall_citations.collect_transcripts(
        home, {}, now - timedelta(days=days), now, diagnostics)
    total, files = result[1], result[-1]
    keep = ("prompts_total", "prompts_with_injection", "injected_claims", "cited_claims",
            "cited_claims_lenient", "cited_injected_rate", "cited_injected_rate_lenient",
            "unique_injected_ids")
    return {"source": "review_recall_citations.collect_transcripts (main Claude transcripts)",
            "files_scanned": files, **{k: total[k] for k in keep}}


def dream_queue(conn: sqlite3.Connection, since: datetime, now: datetime) -> dict:
    queue = dict(conn.execute("SELECT state, COUNT(*) FROM dream_captures GROUP BY state"))
    oldest = conn.execute(
        "SELECT MIN(captured_at) FROM dream_captures WHERE state = 'captured'").fetchone()[0]
    hours = []
    actions: Counter = Counter()
    # first application per capture inside the window; dream_applications is small and unindexed on time
    for captured_at, applied_at, action in conn.execute(
        """SELECT c.captured_at, MIN(a.applied_at), a.action
           FROM dream_applications a JOIN dream_captures c ON c.id = a.capture_id
           GROUP BY a.capture_id HAVING MIN(a.applied_at) >= ?""", (since.isoformat(),)):
        delta = (parse_ts(applied_at) - parse_ts(captured_at)).total_seconds() / 3600
        hours.append(max(delta, 0.0))
        actions[action] += 1
    return {
        "source": "capture-control.db dream_captures / dream_applications",
        "queue_by_status": queue,
        "oldest_captured_age_hours": round((now - parse_ts(oldest)).total_seconds() / 3600, 1) if oldest else None,
        "captures_applied_in_window": len(hours),
        "first_action_of_captures": dict(actions),
        "capture_to_applied_hours_p50": _round(percentile(hours, 0.50)),
        "capture_to_applied_hours_p95": _round(percentile(hours, 0.95)),
        "p95_definition": "nearest-rank over captures whose first application fell in the window; "
                          "captures still unapplied are in queue_by_status, not here",
    }


def _round(value):
    return round(value, 2) if value is not None else None


def hook_telemetry(log: Path, since: datetime) -> dict:
    if not (log.exists() or log.with_suffix(log.suffix + ".1").exists()):
        reason = "recall-dense.jsonl not found; hook-events.jsonl carries only ts/cli/event/count"
        return {"injected_tokens_per_turn": _unavailable(reason), "hook_latency": _unavailable(reason)}
    events = read_events(log, since=since)
    injecting = [int(e.get("chars") or 0) for e in events if int(e.get("chars") or 0) > 0]
    tokens = [c // CHARS_PER_TOKEN for c in injecting]
    totals = [float(e["total_ms"]) for e in events if e.get("total_ms") is not None]
    if not events:
        reason = "no recall-dense.jsonl events in the window"
        return {"injected_tokens_per_turn": _unavailable(reason), "hook_latency": _unavailable(reason)}
    return {
        "injected_tokens_per_turn": {
            "source": "recall-dense.jsonl chars // 4 (estimate); hook-events.jsonl has no token field",
            "events": len(events),
            "injecting_events": len(tokens),
            "zero_injection_events": len(events) - len(tokens),
            "p50": percentile(tokens, 0.50),
            "p95": percentile(tokens, 0.95),
            "scope": "recall injection only, dense path only; SessionStart/other injections not covered",
        },
        "hook_latency": {
            "source": "recall-dense.jsonl total_ms: recall hook internal wall time, excludes process startup",
            "events_with_latency": len(totals),
            "p50_ms": percentile(totals, 0.50),
            "p95_ms": percentile(totals, 0.95),
            "scope": "dense path (Claude Code UserPromptSubmit) only; lexical-only, codex, opencode, agy not covered",
        },
    }


def _section(fn, *args):
    """One metric's failure must not sink the report: record it as null with the reason."""
    try:
        return fn(*args)
    except (sqlite3.Error, OSError, ValueError, KeyError) as exc:
        return _unavailable(f"{type(exc).__name__}: {exc}")


def _with_db(path: Path, fn, *args):
    def run():
        with closing(open_ro(path)) as conn:
            return fn(conn, *args)
    return _section(run)


def collect(db: Path, ledger: Path, home: Path, dense_log: Path, days: int = 7, now: datetime | None = None) -> dict:
    started = time.perf_counter()
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    telemetry = _section(hook_telemetry, dense_log, since)
    return {
        "generated_at": now.isoformat(),
        "window_days": days,
        "window_start": since.isoformat(),
        "recall_returns_by_origin": _with_db(db, machine_notice_share, since),
        "injected_claims_cited": _section(citation_rate, home, days, now),
        "dreaming": _with_db(ledger, dream_queue, since, now),
        **(telemetry if "injected_tokens_per_turn" in telemetry else
           {"injected_tokens_per_turn": telemetry, "hook_latency": telemetry}),
        "elapsed_seconds": round(time.perf_counter() - started, 1),
    }


def default_db() -> Path:
    configured = os.environ.get("MEMORYMASTER_DEFAULT_DB") or os.environ.get("MEMORYMASTER_DB")
    return Path(configured) if configured else REPO_ROOT / "memorymaster.db"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--db", type=Path, default=default_db(), help="claims database (usage_feedback), opened ro")
    parser.add_argument("--ledger-db", type=Path, default=capture_state_path(), help="capture-control.db, opened ro")
    parser.add_argument("--home", type=Path, default=Path.home(), help="home holding .claude/projects transcripts")
    parser.add_argument("--dense-log", type=Path, default=None,
                        help="recall-dense.jsonl (default: <home>/.memorymaster/metrics/recall-dense.jsonl)")
    parser.add_argument("--out", help="write the JSON report here (default: stdout)")
    args = parser.parse_args(argv)
    dense_log = args.dense_log or args.home / ".memorymaster" / "metrics" / "recall-dense.jsonl"
    text = json.dumps(collect(args.db, args.ledger_db, args.home, dense_log, args.days), indent=2, sort_keys=True)
    if args.out:
        out = Path(args.out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
    else:
        sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

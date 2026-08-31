"""Self-contained local JSON and HTML reports without source-path disclosure."""

from __future__ import annotations

import html
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .analysis import VERIFICATION_TIERS
from .storage import SCHEMA_VERSION, WorkflowStore


def _count(store: WorkflowStore, sql: str, params: tuple = ()) -> int:
    return int(store.connection.execute(sql, params).fetchone()[0])


def build_report(
    store: WorkflowStore, *, scope: str | None = None, since: str | None = None,
) -> dict[str, Any]:
    filters: list[str] = []
    params: list[str] = []
    if scope:
        filters.append("project_scope=?")
        params.append(scope)
    if since:
        filters.append("started_at>=?")
        params.append(since)
    where = " WHERE " + " AND ".join(filters) if filters else ""
    sessions = store.connection.execute(
        "SELECT * FROM sessions" + where + " ORDER BY started_at,session_id", tuple(params)
    ).fetchall()
    session_ids = {row["session_id"] for row in sessions}
    feedback = [row for row in store.rows("feedback") if row["session_id"] in session_ids]
    candidates = [dict(row) for row in store.rows("candidates")]
    verification = Counter(row["verification_tier"] for row in sessions)
    completion = Counter(row["completion_state"] for row in sessions)
    correction = Counter(row["theme"] for row in feedback if row["user_origin"])
    lineage = {
        "source_files": _count(store, "SELECT COUNT(*) FROM source_files"),
        "deep_parsed_sessions": sum(bool(row["deep_parsed"]) for row in sessions),
        "scan_runs": _count(store, "SELECT COUNT(*) FROM scan_runs"),
    }
    evidence = [
        {
            "session_ref": row["session_id"][:16],
            "project": next((s["project_scope"] for s in sessions if s["session_id"] == row["session_id"]), "global"),
            "theme": row["theme"], "excerpt": row["excerpt"], "confidence": row["confidence"],
        }
        for row in feedback[:100]
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "filters": {"scope": scope, "since": since},
        "dataset": {
            "sessions": len(sessions),
            "human_sessions": sum(row["session_kind"] in {"human", "mixed"} for row in sessions),
            "subagent_sessions": sum(row["session_kind"] == "subagent" for row in sessions),
            "automation_sessions": sum(row["session_kind"] == "automation" for row in sessions),
            "providers": dict(sorted(Counter(row["provider"] for row in sessions).items())),
        },
        "lineage": lineage,
        "corrections": dict(sorted(correction.items())),
        "verification": {name: verification[name] for name in VERIFICATION_TIERS},
        "completion_states": dict(sorted(completion.items())),
        "retry_loops": sum(_metadata(row).get("retry_loops", 0) for row in sessions),
        "mutation_before_research": sum(bool(_metadata(row).get("mutation_before_research")) for row in sessions),
        "skill_telemetry": {"available": _count(store, "SELECT COUNT(*) FROM source_files WHERE source_kind='skill_outcome'") > 0},
        "a2a_closure": _a2a_summary(store),
        "policy_drift": {"snapshots": _count(store, "SELECT COUNT(*) FROM policy_snapshots")},
        "candidates": candidates,
        "evidence": evidence,
        "unknowns": {
            "unclassified_sessions": sum(row["task_category"] == "unknown" for row in sessions),
            "unknown_completion": completion["unknown"],
            "note": "Silence and completion claims are not treated as acceptance.",
        },
    }


def _metadata(row) -> dict[str, Any]:
    try:
        value = json.loads(row["metadata_json"] or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _a2a_summary(store: WorkflowStore) -> dict[str, int]:
    total = _count(store, "SELECT COUNT(*) FROM source_files WHERE source_kind='wezbridge'")
    sessions = _count(store, "SELECT COUNT(*) FROM sessions WHERE provider='wezbridge'")
    rows = store.connection.execute(
        "SELECT metadata_json FROM sessions WHERE provider='wezbridge'"
    ).fetchall()
    closed = 0
    result_without_ack = 0
    unknown = 0
    for row in rows:
        try:
            statuses = set(json.loads(row["metadata_json"] or "{}").get("a2a_types") or [])
        except (json.JSONDecodeError, AttributeError):
            statuses = set()
        closed += int("result" in statuses and "ack" in statuses)
        result_without_ack += int("result" in statuses and "ack" not in statuses)
        unknown += int(not statuses)
    return {
        "sources": total, "threads_observed": sessions, "closed_result_and_ack": closed,
        "result_without_ack": result_without_ack, "closure_unknown": unknown,
    }


def write_report(report: dict[str, Any], output_dir: str | Path) -> dict[str, Path]:
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    json_path = target / "report.json"
    html_path = target / "report.html"
    json_path.write_text(json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    html_path.write_text(_render_html(report), encoding="utf-8")
    return {"json": json_path, "html": html_path}


def _render_html(report: dict[str, Any]) -> str:
    sections = [
        ("Dataset", report["dataset"]), ("Lineage", report["lineage"]),
        ("Corrections", report["corrections"]), ("Verification", report["verification"]),
        ("Completion states", report["completion_states"]),
        ("Skill telemetry", report["skill_telemetry"]), ("A2A closure", report["a2a_closure"]),
        ("Policy drift", report["policy_drift"]), ("Candidates", report["candidates"]),
        ("Unknowns", report["unknowns"]),
    ]
    cards = "".join(
        f"<section><h2>{html.escape(title)}</h2><pre>{html.escape(json.dumps(value, indent=2, sort_keys=True))}</pre></section>"
        for title, value in sections
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MemoryMaster Workflow Intelligence</title><style>
body{{font:15px/1.5 system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;background:#f6f7f9;color:#18202a}}
h1{{font-size:2rem}}section{{background:white;border:1px solid #dce1e7;border-radius:10px;padding:1rem;margin:1rem 0}}
pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#f8fafc;padding:.8rem;border-radius:6px}}
</style></head><body><h1>MemoryMaster Workflow Intelligence</h1>
<p>Generated {html.escape(str(report['generated_at']))}. Deterministic evidence unless explicitly labeled otherwise.</p>{cards}</body></html>"""


__all__ = ["build_report", "write_report"]

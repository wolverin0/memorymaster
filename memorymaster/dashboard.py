from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections import Counter, deque
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from memorymaster.config import get_config
from memorymaster.review import build_review_queue, queue_to_dicts
from memorymaster.security import is_sensitive_claim
from memorymaster.service import MemoryService
import contextlib


def _first_query_value(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    if not values:
        return None
    value = str(values[-1]).strip()
    return value if value else None


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean value: {value!r}")


def _parse_int(value: str | None, *, default: int, minimum: int, maximum: int) -> int:
    if value is None:
        return default
    parsed = int(value)
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"Expected integer in range [{minimum}, {maximum}], got {parsed}")
    return parsed


def _tail_events_from_jsonl(path: Path, limit: int) -> list[dict[str, Any]]:
    if limit <= 0 or not path.exists():
        return []
    rows: deque[dict[str, Any]] = deque(maxlen=limit)
    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip().lstrip("\ufeff")
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                rows.append(parsed)
    return list(rows)


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    if quantile <= 0.0:
        return min(values)
    if quantile >= 1.0:
        return max(values)
    ordered = sorted(float(value) for value in values)
    index = (len(ordered) - 1) * quantile
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    weight = index - lower
    return (ordered[lower] * (1.0 - weight)) + (ordered[upper] * weight)


def _latency_summary_from_rows(rows: list[dict[str, Any]]) -> dict[str, dict[str, float | int]]:
    by_metric: dict[str, list[float]] = {}
    for row in rows:
        for key, value in row.items():
            if key.endswith("_ms") and isinstance(value, (int, float)) and not isinstance(value, bool):
                by_metric.setdefault(key, []).append(float(value))
    return {
        name: {
            "count": len(values),
            "p50": (_percentile(values, 0.50) or 0.0),
            "p95": (_percentile(values, 0.95) or 0.0),
            "avg": (sum(values) / max(1, len(values))),
            "max": (max(values) if values else 0.0),
        }
        for name, values in sorted(by_metric.items())
    }


def _claim_to_dict(claim: Any) -> dict[str, Any]:
    return asdict(claim)


def _event_to_dict(event: Any) -> dict[str, Any]:
    payload = asdict(event)
    raw = payload.get("payload_json")
    parsed = None
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
    payload["payload"] = parsed
    return payload


class DashboardHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        service: MemoryService,
        operator_log_jsonl: str | Path,
        db_target: str | Path | None,
        workspace_root: str | Path | None,
    ) -> None:
        self.service = service
        self.operator_log_jsonl = Path(operator_log_jsonl)
        self.db_target = str(db_target) if db_target is not None else "memorymaster.db"
        self.workspace_root = Path(workspace_root) if workspace_root is not None else Path.cwd()
        self._operator_proc: subprocess.Popen[str] | None = None
        super().__init__(server_address, DashboardRequestHandler)

    def operator_status(self) -> dict[str, Any]:
        proc = self._operator_proc
        running = proc is not None and proc.poll() is None
        return {
            "running": running,
            "pid": (proc.pid if running and proc is not None else None),
            "log_jsonl": str(self.operator_log_jsonl),
        }

    def start_operator(self, inbox_jsonl: str) -> dict[str, Any]:
        if self.operator_status()["running"]:
            return {"started": False, "reason": "already_running", **self.operator_status()}
        inbox = Path(inbox_jsonl)
        inbox.parent.mkdir(parents=True, exist_ok=True)
        inbox.touch(exist_ok=True)
        self.operator_log_jsonl.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            sys.executable,
            "-m",
            "memorymaster",
            "--db",
            self.db_target,
            "--workspace",
            str(self.workspace_root),
            "run-operator",
            "--inbox-jsonl",
            str(inbox),
            "--max-idle-seconds",
            "120",
            "--retrieval-mode",
            "hybrid",
            "--policy-mode",
            "cadence",
            "--log-jsonl",
            str(self.operator_log_jsonl),
        ]
        self._operator_proc = subprocess.Popen(  # noqa: S603
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(self.workspace_root),
            text=True,
        )
        return {"started": True, **self.operator_status()}

    def stop_operator(self) -> dict[str, Any]:
        proc = self._operator_proc
        if proc is None or proc.poll() is not None:
            self._operator_proc = None
            return {"stopped": False, "reason": "not_running", **self.operator_status()}
        try:
            proc.terminate()
            proc.wait(timeout=4)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except Exception:
                pass
        self._operator_proc = None
        return {"stopped": True, **self.operator_status()}


class DashboardRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:
        return

    @property
    def _server(self) -> DashboardHTTPServer:
        return self.server  # type: ignore[return-value]

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        route = parsed.path
        try:
            if route == "/health":
                self._write_json({"ok": True, "service": "memorymaster-dashboard"})
                return
            if route in {"/", "/dashboard"}:
                self._write_dashboard()
                return
            if route == "/api/claims":
                self._handle_claims(parsed.query)
                return
            if route == "/api/events":
                self._handle_events(parsed.query)
                return
            if route == "/api/timeline":
                self._handle_timeline(parsed.query)
                return
            if route == "/api/conflicts":
                self._handle_conflicts(parsed.query)
                return
            if route == "/api/review-queue":
                self._handle_review_queue(parsed.query)
                return
            if route == "/api/retrieval":
                self._handle_retrieval(parsed.query)
                return
            if route == "/api/audit":
                self._handle_audit(parsed.query)
                return
            if route == "/api/namespaces":
                self._handle_namespaces(parsed.query)
                return
            if route == "/api/session-stats":
                self._handle_session_stats(parsed.query)
                return
            if route == "/api/observability":
                self._handle_observability(parsed.query)
                return
            if route == "/api/operator/status":
                self._write_json({"ok": True, **self._server.operator_status()})
                return
            if route == "/api/operator/stream":
                self._handle_operator_stream(parsed.query)
                return
            self._write_json({"ok": False, "error": "Not found"}, status=HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self._write_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self._write_json({"ok": False, "error": f"Internal server error: {exc}"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        route = parsed.path
        try:
            payload = self._read_json_body()
            if route == "/api/triage/action":
                self._handle_triage_action(payload)
                return
            if route == "/api/operator/control":
                self._handle_operator_control(payload)
                return
            self._write_json({"ok": False, "error": "Not found"}, status=HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self._write_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self._write_json({"ok": False, "error": f"Internal server error: {exc}"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        data = self.rfile.read(length)
        parsed = json.loads(data.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("Expected JSON object body")
        return parsed

    def _write_json(self, payload: dict[str, Any], *, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _write_dashboard(self) -> None:
        html = """<!doctype html>
<html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>MemoryMaster Dashboard</title>
<style>
*{box-sizing:border-box}
body{font-family:'Segoe UI',system-ui,-apple-system,sans-serif;margin:0;background:#0f172a;color:#e2e8f0}
main{max-width:1400px;margin:0 auto;padding:20px}
.header{display:flex;align-items:center;gap:16px;margin-bottom:24px;padding-bottom:16px;border-bottom:1px solid #1e293b}
.header h1{margin:0;font-size:1.5rem;color:#f8fafc;letter-spacing:-0.02em}
.header .logo{width:36px;height:36px;background:linear-gradient(135deg,#3b82f6,#8b5cf6);border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:18px;font-weight:bold;color:#fff}
.header .subtitle{color:#64748b;font-size:.85rem;margin-top:2px}
.header .version{margin-left:auto;background:#1e293b;color:#94a3b8;padding:4px 10px;border-radius:20px;font-size:.75rem;font-family:ui-monospace,monospace}
.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:16px}
section{background:#1e293b;border:1px solid #334155;border-radius:12px;padding:16px;transition:border-color .2s;min-width:0}
.wide{grid-column:1/-1}
section:hover{border-color:#475569}
.section-head{display:flex;align-items:center;gap:8px;margin-bottom:10px}
.section-head .icon{width:28px;height:28px;border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:14px}
.section-head h2{margin:0;font-size:.95rem;color:#f1f5f9;font-weight:600}
.section-head .desc{color:#64748b;font-size:.78rem;margin-top:1px}
.muted{color:#64748b;font-size:.85rem}
.empty{color:#475569;font-style:italic;padding:12px;text-align:center}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.85rem}
.scroll{max-height:300px;overflow:auto;border:1px solid #334155;border-radius:8px}
table{width:100%;border-collapse:collapse;font-size:.85rem;table-layout:fixed}
th,td{padding:8px 10px;border-bottom:1px solid #334155;text-align:left;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
td.wrap{white-space:normal;word-break:break-word}
th{background:#0f172a;color:#94a3b8;font-weight:500;position:sticky;top:0;text-transform:uppercase;font-size:.72rem;letter-spacing:.05em}
td{color:#cbd5e1}
tr:hover td{background:rgba(59,130,246,.06)}
button{border:1px solid #475569;background:#1e293b;color:#e2e8f0;border-radius:6px;padding:5px 12px;cursor:pointer;font-size:.8rem;transition:all .15s}
button:hover{background:#334155;border-color:#64748b}
.primary{background:#3b82f6;color:#fff;border-color:#2563eb}
.primary:hover{background:#2563eb}
.danger{background:#ef4444;color:#fff;border-color:#dc2626}
.danger:hover{background:#dc2626}
pre.stream{margin:0;max-height:220px;overflow:auto;background:#020617;color:#22d3ee;padding:12px;border-radius:8px;font-size:.8rem;line-height:1.5;border:1px solid #0e7490}
.toolbar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:8px 0}
.toolbar input,.toolbar select{padding:6px 10px;border:1px solid #475569;border-radius:8px;background:#0f172a;color:#e2e8f0;font-size:.82rem}
.toolbar input::placeholder{color:#475569}
.toolbar input:focus,.toolbar select:focus{outline:none;border-color:#3b82f6;box-shadow:0 0 0 2px rgba(59,130,246,.2)}
.badge{display:inline-block;border-radius:12px;padding:2px 8px;font-size:.72rem;font-weight:500;letter-spacing:.02em}
.badge-candidate{background:#1e3a5f;color:#60a5fa;border:1px solid #2563eb}
.badge-confirmed{background:#14532d;color:#4ade80;border:1px solid #16a34a}
.badge-stale{background:#451a03;color:#fb923c;border:1px solid #d97706}
.badge-conflicted{background:#450a0a;color:#f87171;border:1px solid #dc2626}
.badge-superseded{background:#312e81;color:#a78bfa;border:1px solid #7c3aed}
.badge-archived{background:#1e293b;color:#64748b;border:1px solid #475569}
.card{border:1px solid #334155;border-radius:8px;margin:8px 0;background:#1e293b}
.card-head{padding:8px 10px;border-bottom:1px solid #2d3a4e;background:rgba(15,23,42,.4);border-radius:8px 8px 0 0}
.card-row{padding:8px 10px;border-bottom:1px solid #273348}
.stat-row{display:flex;gap:16px;flex-wrap:wrap;padding:8px 0}
.stat-item{display:flex;flex-direction:column;gap:2px}
.stat-item .stat-value{font-size:1.1rem;font-weight:600;color:#f8fafc;font-family:ui-monospace,monospace}
.stat-item .stat-label{font-size:.7rem;color:#64748b;text-transform:uppercase;letter-spacing:.05em}
.icon-blue{background:rgba(59,130,246,.15);color:#60a5fa}
.icon-green{background:rgba(34,197,94,.15);color:#4ade80}
.icon-amber{background:rgba(245,158,11,.15);color:#fbbf24}
.icon-red{background:rgba(239,68,68,.15);color:#f87171}
.icon-purple{background:rgba(139,92,246,.15);color:#a78bfa}
.icon-cyan{background:rgba(6,182,212,.15);color:#22d3ee}
.icon-slate{background:rgba(100,116,139,.15);color:#94a3b8}
.icon-pink{background:rgba(236,72,153,.15);color:#f472b6}
.icon-teal{background:rgba(20,184,166,.15);color:#2dd4bf}
.icon-indigo{background:rgba(99,102,241,.15);color:#818cf8}
.icon-lime{background:rgba(132,204,22,.15);color:#a3e635}
input[type="text"],input:not([type]){background:#0f172a;border:1px solid #475569;color:#e2e8f0;border-radius:8px;padding:6px 10px}
</style></head><body><main>
<div class="header">
<div class="logo">M</div>
<div><h1>MemoryMaster</h1><div class="subtitle">AI Agent Memory Management Dashboard</div></div>
<span class="version">v1.0.0</span>
</div>
<div class="grid">
<section>
<div class="section-head"><div class="icon icon-green">&#9654;</div><div><h2>Operator</h2><div class="desc">Start or stop the memory operator process</div></div></div>
<div class="toolbar"><input id="op-inbox" value="artifacts/operator/operator_inbox.jsonl" style="flex:1"> <button id="op-start" class="primary">&#9654; Start</button> <button id="op-stop" class="danger">&#9632; Stop</button></div>
<div id="op-status" class="muted" style="margin-top:6px">Checking status...</div>
</section>
<section>
<div class="section-head"><div class="icon icon-cyan">&#128200;</div><div><h2>System Health</h2><div class="desc">Operator metrics, latency, event counters</div></div></div>
<div id="obs-box" class="scroll"><div class="empty">Waiting for data...</div></div>
</section>
<section class="wide">
<div class="section-head"><div class="icon icon-blue">&#128203;</div><div><h2>Claims</h2><div class="desc">All stored knowledge claims with status and confidence</div></div></div>
<div class="scroll"><table><colgroup><col style="width:50px"><col style="width:90px"><col><col style="width:80px"><col style="width:60px"><col style="width:160px"></colgroup><thead><tr><th>ID</th><th>Status</th><th>Subject / Predicate / Value</th><th>Confidence</th><th>Cites</th><th>Updated</th></tr></thead><tbody id="claims-body"><tr><td colspan="6" class="empty">No claims ingested yet</td></tr></tbody></table></div>
</section>
<section>
<div class="section-head"><div class="icon icon-purple">&#128337;</div><div><h2>Timeline</h2><div class="desc">Event feed — transitions, validations, policy decisions</div></div></div>
<div class="toolbar"><input id="timeline-search" placeholder="Filter events..."> <select id="timeline-event-filter"><option value="">All types</option><option value="transition">Transitions</option><option value="validator">Validators</option><option value="deterministic_validator">Deterministic</option><option value="policy_decision">Policy</option><option value="audit">Audit</option></select> <select id="timeline-group"><option value="claim">Group by claim</option><option value="event_type">Group by type</option></select></div><div id="timeline-meta" class="muted"></div><div id="timeline-list" class="scroll"><div class="empty">No events recorded yet</div></div>
</section>
<section>
<div class="section-head"><div class="icon icon-red">&#9888;</div><div><h2>Conflicts</h2><div class="desc">Claims that contradict each other — side-by-side comparison</div></div></div>
<div class="toolbar"><input id="conflicts-search" placeholder="Search conflicts..."> <label class="muted" style="display:flex;align-items:center;gap:4px"><input id="conflicts-include-stale" type="checkbox"> Include stale</label> <button id="conflicts-refresh">Refresh</button></div><div id="conflicts-meta" class="muted"></div><div id="conflicts-cards" class="scroll"><div class="empty">No conflicts detected</div></div>
</section>
<section class="wide">
<div class="section-head"><div class="icon icon-amber">&#128221;</div><div><h2>Review Queue</h2><div class="desc">Stale or flagged claims that need human review</div></div></div>
<div class="scroll"><table><thead><tr><th>Claim</th><th>Status</th><th>Reason</th><th>Priority</th><th>Actions</th></tr></thead><tbody id="stale-body"><tr><td colspan="5" class="empty">Nothing to review</td></tr></tbody></table></div>
</section>
<section class="wide">
<div class="section-head"><div class="icon icon-teal">&#128269;</div><div><h2>Search</h2><div class="desc">Query claims using hybrid retrieval (lexical + vector + freshness)</div></div></div>
<div class="toolbar"><input id="retrieval-query" placeholder="What are you looking for?" style="flex:1"> <select id="retrieval-mode"><option value="hybrid">Hybrid</option><option value="legacy">Legacy</option></select> <input id="retrieval-scope" placeholder="Scopes (optional)" style="width:140px"> <button id="retrieval-run" class="primary">&#128269; Search</button></div><div id="retrieval-meta" class="muted"></div><div class="scroll"><table><thead><tr><th>ID</th><th>Subject / Predicate / Value</th><th>Status</th><th>Annotation</th><th>Score</th><th>Breakdown</th></tr></thead><tbody id="retrieval-body"><tr><td colspan="6" class="empty">Enter a query and click Search</td></tr></tbody></table></div>
</section>
<section>
<div class="section-head"><div class="icon icon-slate">&#128274;</div><div><h2>Audit Log</h2><div class="desc">Security and governance audit trail</div></div></div>
<div class="scroll"><table><thead><tr><th>Time</th><th>Event</th><th>Claim</th><th>Details</th></tr></thead><tbody id="audit-body"><tr><td colspan="4" class="empty">No audit events</td></tr></tbody></table></div>
</section>
<section>
<div class="section-head"><div class="icon icon-indigo">&#128194;</div><div><h2>Namespaces</h2><div class="desc">Logical groupings of claims by scope</div></div></div>
<div id="namespaces-box" class="scroll"><div class="empty">No namespaces created yet</div></div>
</section>
<section>
<div class="section-head"><div class="icon icon-pink">&#128202;</div><div><h2>Session Stats</h2><div class="desc">Operator session and thread activity</div></div></div>
<div id="session-stats" class="scroll"><div class="empty">No sessions recorded</div></div>
</section>
<section style="grid-column:1/-1">
<div class="section-head"><div class="icon icon-lime">&#9889;</div><div><h2>Live Stream</h2><div class="desc">Real-time operator events via Server-Sent Events</div></div></div>
<pre id="stream" class="stream">Waiting for operator to start...</pre>
</section>
</div>
<script>
const esc=(v)=>String(v==null?'':v).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'","&#39;");
const statusBadge=(s)=>{const v=String(s||'unknown').toLowerCase();const cls=({candidate:'badge-candidate',stale:'badge-stale',conflicted:'badge-conflicted',confirmed:'badge-confirmed',archived:'badge-archived',superseded:'badge-superseded'})[v]||'';return '<span class="badge '+cls+'">'+esc(v)+'</span>';};
const f3=(v)=>typeof v==='number'?v.toFixed(3):'-';
const tuple=(c)=>'<span class="mono">'+esc(c.subject||'-')+' / '+esc(c.predicate||'-')+' / '+esc(c.object_value||c.text||'-')+'</span>';
async function jget(p){const r=await fetch(p);const d=await r.json();if(!r.ok||d.ok===false)throw new Error((d&&d.error)||('HTTP '+r.status));return d;}
async function jpost(p,b){const r=await fetch(p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})});const d=await r.json();if(!r.ok||d.ok===false)throw new Error((d&&d.error)||('HTTP '+r.status));return d;}
const timelineState={rows:[],search:'',eventType:'',group:'claim'};
const conflictState={rows:[],search:''};
const topMap=(obj,limit)=>Object.entries(obj||{}).sort((a,b)=>Number(b[1]||0)-Number(a[1]||0)).slice(0,limit);
const actor=(e)=>{const p=e&&typeof e.payload==='object'?e.payload:null;const src=p&&p.source?String(p.source).trim():'';if(src)return src;if(String(e.event_type||'')==='policy_decision')return 'policy';if(String(e.event_type||'')==='validator'||String(e.event_type||'')==='deterministic_validator')return 'validator';return 'system';};
const countPills=(obj,limit)=>{const rows=topMap(obj,limit);return rows.length?rows.map(([k,v])=>'<span class="mono">'+esc(k)+'='+esc(v)+'</span>').join(' | '):'<span class="empty" style="display:inline;padding:0">none</span>';};
function fillClaims(d){const rows=Array.isArray(d.claims)?d.claims:[];const b=document.getElementById('claims-body');if(!rows.length){b.innerHTML='<tr><td colspan="6" class="empty">No claims ingested yet</td></tr>';return;}b.innerHTML=rows.map(c=>'<tr><td class="mono">'+esc(c.id)+'</td><td>'+statusBadge(c.status)+'</td><td class="wrap">'+tuple(c)+'</td><td class="mono">'+esc(f3(c.confidence))+'</td><td class="mono">'+esc((c.citations||[]).length)+'</td><td class="mono">'+esc(c.updated_at||'-')+'</td></tr>').join('');}
function renderTimeline(){const h=document.getElementById('timeline-list');const meta=document.getElementById('timeline-meta');const icon=(t)=>({ingest:'&#128229;',extractor:'&#128268;',validator:'&#9989;',deterministic_validator:'&#128170;',transition:'&#128260;',policy_decision:'&#9878;',audit:'&#128274;',compaction_run:'&#128465;',supersession:'&#128260;',confidence:'&#128200;'})[String(t||'')]||'&#128312;';const q=String(timelineState.search||'').toLowerCase().trim();const eventType=String(timelineState.eventType||'').trim();const rows=timelineState.rows.filter(e=>{if(eventType&&String(e.event_type||'')!==eventType)return false;if(!q)return true;const hay=[e.event_type,e.details,e.from_status,e.to_status,e.claim_id,actor(e),e.created_at].map(v=>String(v||'').toLowerCase()).join(' ');return hay.includes(q);});meta.textContent=rows.length+' of '+timelineState.rows.length+' events';if(!rows.length){h.innerHTML='<div class="empty">No matching events</div>';return;}const byDay={};rows.forEach(e=>{const day=String(e.created_at||'').slice(0,10)||'unknown';if(!byDay[day])byDay[day]=[];byDay[day].push(e);});h.innerHTML=Object.keys(byDay).sort().reverse().map(day=>{const buckets={};byDay[day].forEach(e=>{const key=(timelineState.group==='event_type')?('event:'+String(e.event_type||'event')):(e.claim_id?('claim:'+String(e.claim_id)):('event:'+String(e.event_type||'event')));if(!buckets[key])buckets[key]=[];buckets[key].push(e);});const bucketHtml=Object.keys(buckets).sort().map(key=>{const evs=buckets[key];return '<div class="card"><div class="card-head"><span class="mono">'+esc(key)+'</span> <span class="muted">('+esc(evs.length)+' events)</span></div>'+evs.map(e=>{const a=actor(e);return '<div class="card-row"><div class="mono muted" style="font-size:.75rem">'+esc(e.created_at||'-')+'</div><div>'+icon(e.event_type)+' <strong>'+esc(e.event_type||'event')+'</strong> '+statusBadge(e.to_status||e.event_type)+' <span class="muted">by '+esc(a)+'</span> '+(e.claim_id?('<span class="muted">&#183; claim #'+esc(e.claim_id)+'</span>'):'')+'</div>'+(e.from_status&&e.to_status?'<div class="muted" style="font-size:.8rem">'+esc(e.from_status)+' &#8594; '+esc(e.to_status)+'</div>':'')+(e.details?('<div class="muted" style="font-size:.8rem">'+esc(e.details)+'</div>'):'')+'</div>';}).join('')+'</div>';}).join('');return '<div><div style="position:sticky;top:0;background:#0f172a;padding:6px 10px;border-bottom:1px solid #334155;z-index:1"><strong style="color:#f8fafc">'+esc(day)+'</strong> <span class="muted">('+esc(byDay[day].length)+' events)</span></div>'+bucketHtml+'</div>';}).join('');}
function fillTimeline(d){timelineState.rows=Array.isArray(d.timeline)?d.timeline:[];renderTimeline();}
function renderConflicts(){const h=document.getElementById('conflicts-cards');const meta=document.getElementById('conflicts-meta');const q=String(conflictState.search||'').toLowerCase().trim();const rows=conflictState.rows.filter(g=>{if(!q)return true;const claims=Array.isArray(g.claims)?g.claims:[];const claimText=claims.map(c=>[c.id,c.status,c.subject,c.predicate,c.object_value,c.text].join(' ')).join(' ').toLowerCase();return ([g.subject,g.predicate,g.scope].join(' ').toLowerCase()+' '+claimText).includes(q);});meta.textContent=rows.length+' of '+conflictState.rows.length+' conflict groups';h.innerHTML=rows.map(g=>{const cs=Array.isArray(g.claims)?g.claims:[];const n=cs[0]||null;const o=cs.length>1?cs[1]:null;const nv=n?(n.object_value||n.text||'-'):'-';const ov=o?(o.object_value||o.text||'-'):'-';const nc=n?Number(n.confidence||0):null;const oc=o?Number(o.confidence||0):null;const nz=n?((n.citations||[]).length):0;const oz=o?((o.citations||[]).length):0;const confDelta=(nc!=null&&oc!=null)?(nc-oc):null;const citeDelta=(n&&o)?(nz-oz):null;const valueChanged=(String(nv)!==String(ov));const delta=(v)=>v==null?'-':((v>=0?'+':'')+Number(v).toFixed(3));const cDelta=(v)=>v==null?'-':((v>=0?'+':'')+String(v));const row=(label,a,b,chg)=>'<tr><td>'+esc(label)+'</td><td class="mono">'+esc(a)+'</td><td class="mono">'+esc(b)+'</td><td class="mono">'+esc(chg)+'</td></tr>';const statusCounts={};cs.forEach(c=>{const k=String(c.status||'unknown');statusCounts[k]=(statusCounts[k]||0)+1;});return '<div style="padding:10px;border-bottom:1px solid #334155"><div class="mono" style="color:#f8fafc"><strong>'+esc(g.subject||'-')+' / '+esc(g.predicate||'-')+'</strong></div><div class="muted">'+esc(cs.length)+' claims &#183; scope: '+esc(g.scope||'project')+' &#183; '+countPills(statusCounts,4)+'</div><div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:8px"><div style="border:1px solid #334155;border-radius:8px;padding:8px;background:#0f172a"><div><strong style="color:#4ade80">Newer</strong> '+(n?statusBadge(n.status):'')+' <span class="mono muted">#'+esc(n?n.id:'-')+'</span></div><div class="mono" style="color:#f8fafc;margin:4px 0">'+esc(nv)+'</div><div class="muted" style="font-size:.78rem">confidence: '+esc(nc==null?'-':nc.toFixed(3))+' &#183; citations: '+esc(nz)+'</div></div><div style="border:1px solid #334155;border-radius:8px;padding:8px;background:#0f172a"><div><strong style="color:#f87171">Older</strong> '+(o?statusBadge(o.status):'')+' <span class="mono muted">#'+esc(o?o.id:'-')+'</span></div><div class="mono" style="color:#f8fafc;margin:4px 0">'+esc(ov)+'</div><div class="muted" style="font-size:.78rem">confidence: '+esc(oc==null?'-':oc.toFixed(3))+' &#183; citations: '+esc(oz)+'</div></div></div><div style="margin-top:8px"><table><thead><tr><th>Field</th><th>Newer</th><th>Older</th><th>Delta</th></tr></thead><tbody>'+row('value',nv,ov,valueChanged?'CHANGED':'same')+row('confidence',nc==null?'-':nc.toFixed(3),oc==null?'-':oc.toFixed(3),delta(confDelta))+row('citations',String(nz),String(oz),cDelta(citeDelta))+row('updated_at',n&&n.updated_at?n.updated_at:'-',o&&o.updated_at?o.updated_at:'-',(n&&o&&String(n.updated_at)!==String(o.updated_at))?'changed':'same')+'</tbody></table></div></div>';}).join('')||'<div class="empty">No conflicts detected</div>';}
function fillConflicts(d){conflictState.rows=Array.isArray(d.groups)?d.groups:[];renderConflicts();}
async function refreshConflicts(){const includeStale=document.getElementById('conflicts-include-stale').checked?'1':'0';fillConflicts(await jget('/api/conflicts?limit=20&include_stale='+includeStale));}
function fillQueue(d){const rows=Array.isArray(d.items)?d.items:[];const b=document.getElementById('stale-body');if(!rows.length){b.innerHTML='<tr><td colspan="5" class="empty">Nothing to review</td></tr>';return;}b.innerHTML=rows.map(i=>'<tr data-claim-id="'+esc(i.claim_id)+'"><td class="mono">'+esc(i.claim_id)+'</td><td>'+statusBadge(i.status)+'</td><td>'+esc(i.reason||'-')+'</td><td class="mono">'+esc(f3(i.priority))+'</td><td style="display:flex;gap:4px;flex-wrap:wrap"><button data-action="pin">&#128204; Pin</button> <button data-action="mark_reviewed">&#9989; Reviewed</button> <button data-action="suppress">&#128683; Suppress</button> <button data-action="approve_proposal" class="primary">&#9989; Approve</button> <button data-action="reject_proposal" class="danger">&#10060; Reject</button></td></tr>').join('');}
function fillRetr(d){const rows=Array.isArray(d.rows_data)?d.rows_data:[];const b=document.getElementById('retrieval-body');const meta=document.getElementById('retrieval-meta');const scopes=Array.isArray(d.scope_allowlist)?d.scope_allowlist:[];const scopeText=scopes.length?scopes.join(', '):'all';meta.textContent='Mode: '+(d.mode||'-')+' &#183; Scopes: '+scopeText+' &#183; '+rows.length+' results';b.innerHTML=rows.map(r=>{const c=r.claim||{};const s=r.status||c.status||'unknown';const ann=(r.annotation||'-');return '<tr><td class="mono">'+esc(c.id)+'</td><td>'+tuple(c)+'</td><td>'+statusBadge(s)+'</td><td>'+esc(ann)+'</td><td class="mono">'+esc(f3(r.score))+'</td><td class="mono" style="font-size:.75rem">'+esc(f3(r.lexical_score))+' / '+esc(f3(r.confidence_score))+' / '+esc(f3(r.freshness_score))+' / '+esc(f3(r.vector_score))+'</td></tr>';}).join('')||'<tr><td colspan="6" class="empty">No results found</td></tr>';}
function fillAudit(d){const rows=Array.isArray(d.events)?d.events:[];document.getElementById('audit-body').innerHTML=rows.map(e=>'<tr><td class="mono" style="font-size:.75rem">'+esc(e.created_at||'-')+'</td><td>'+esc(e.event_type||'-')+'</td><td class="mono">'+esc(e.claim_id||'-')+'</td><td>'+esc(e.details||'-')+'</td></tr>').join('')||'<tr><td colspan="4" class="empty">No audit events</td></tr>';}
function fillNs(d){const ns=d.namespaces||{};const keys=Object.keys(ns);document.getElementById('namespaces-box').innerHTML=keys.length?keys.map(k=>'<div style="padding:8px 10px;border-bottom:1px solid #334155;display:flex;justify-content:space-between"><span style="color:#f1f5f9">'+esc(k)+'</span><span class="mono muted">'+esc(ns[k].count||0)+' claims</span></div>').join(''):'<div class="empty">No namespaces created yet</div>';}
function fillStats(d){const s=d.summary||{};document.getElementById('session-stats').innerHTML='<div style="padding:10px"><div class="stat-row"><div class="stat-item"><span class="stat-value">'+esc(s.sessions||0)+'</span><span class="stat-label">Sessions</span></div><div class="stat-item"><span class="stat-value">'+esc(s.threads||0)+'</span><span class="stat-label">Threads</span></div><div class="stat-item"><span class="stat-value">'+esc(s.rows_scanned||0)+'</span><span class="stat-label">Rows scanned</span></div></div>'+(Object.keys(s.event_counts||{}).length?'<div class="muted" style="margin-top:6px;padding-top:6px;border-top:1px solid #334155">Events: '+countPills(s.event_counts||{},8)+'</div>':'')+'</div>';}
function fillObs(d){const o=d.observability||{};const op=o.operator||{};const ev=o.events_recent||{};const q=o.queue||{};const latency=op.latency_ms||{};const latencyRows=Object.keys(latency).sort().map(k=>'<tr><td class="mono">'+esc(k)+'</td><td class="mono">'+esc(latency[k].count||0)+'</td><td class="mono">'+esc(f3(latency[k].p50))+'</td><td class="mono">'+esc(f3(latency[k].p95))+'</td><td class="mono">'+esc(f3(latency[k].max))+'</td></tr>').join('')||'<tr><td colspan="5" class="empty">No latency data yet</td></tr>';const topQueue=Array.isArray(q.top)?q.top:[];document.getElementById('obs-box').innerHTML='<div style="padding:10px;border-bottom:1px solid #334155"><div class="stat-row"><div class="stat-item"><span class="stat-value">'+(op.running?'<span style="color:#4ade80">&#9679; Running</span>':'<span style="color:#64748b">&#9679; Stopped</span>')+'</span><span class="stat-label">Operator</span></div>'+(op.running?'<div class="stat-item"><span class="stat-value">'+esc(op.pid)+'</span><span class="stat-label">PID</span></div>':'')+'<div class="stat-item"><span class="stat-value">'+esc(op.rows_scanned||0)+'</span><span class="stat-label">Rows</span></div><div class="stat-item"><span class="stat-value">'+esc(op.sessions||0)+'</span><span class="stat-label">Sessions</span></div></div></div><div style="padding:8px 10px;border-bottom:1px solid #334155"><span class="muted">Events:</span> '+countPills(op.event_counts||{},8)+'</div><div style="padding:8px 10px;border-bottom:1px solid #334155"><span class="muted">Tools:</span> '+countPills(op.tool_counts||{},8)+'</div><div style="padding:8px 10px;border-bottom:1px solid #334155"><span class="muted">Queue:</span> <span class="mono">total='+esc(q.rows_scanned||0)+' actionable='+esc(q.actionable||0)+' reviewed='+esc(q.triage_reviewed||0)+' suppressed='+esc(q.triage_suppressed||0)+'</span></div><div style="padding:8px 10px;border-bottom:1px solid #334155"><span class="muted">Priority queue:</span> '+(topQueue.length?topQueue.map(t=>'<span class="mono">#'+esc(t.claim_id)+' '+statusBadge(t.status)+' p='+esc(f3(t.priority))+'</span>').join(' '):'<span class="muted">empty</span>')+'</div><div style="padding:8px 10px"><table><thead><tr><th>Metric</th><th>Samples</th><th>p50</th><th>p95</th><th>Max</th></tr></thead><tbody>'+latencyRows+'</tbody></table></div>';}
function fillOp(d){document.getElementById('op-status').innerHTML=d.running?'<span style="color:#4ade80">&#9679; Running</span> <span class="mono muted">PID '+esc(d.pid)+'</span>':'<span style="color:#64748b">&#9679; Stopped</span>';}
async function refreshQueue(){fillQueue(await jget('/api/review-queue?limit=30&exclude_reviewed=1&exclude_suppressed=1'));}
async function refreshObs(){fillObs(await jget('/api/observability?log_limit=1500&event_limit=600&queue_limit=250'));}
document.getElementById('stale-body').addEventListener('click',async(ev)=>{const t=ev.target;if(!t||t.tagName!=='BUTTON')return;const r=t.closest('tr');if(!r)return;const id=Number(r.getAttribute('data-claim-id'));const a=String(t.getAttribute('data-action')||'');r.remove();await jpost('/api/triage/action',{claim_id:id,action:a});await refreshQueue();});
document.getElementById('op-start').addEventListener('click',async()=>{await jpost('/api/operator/control',{action:'start',inbox_jsonl:document.getElementById('op-inbox').value});fillOp(await jget('/api/operator/status'));});
document.getElementById('op-stop').addEventListener('click',async()=>{await jpost('/api/operator/control',{action:'stop'});fillOp(await jget('/api/operator/status'));});
document.getElementById('retrieval-run').addEventListener('click',async()=>{const query=document.getElementById('retrieval-query').value||'';const mode=document.getElementById('retrieval-mode').value||'hybrid';const scopeRaw=document.getElementById('retrieval-scope').value||'';const url='/api/retrieval?query='+encodeURIComponent(query)+'&mode='+encodeURIComponent(mode)+'&scope_allowlist='+encodeURIComponent(scopeRaw)+'&limit=10';fillRetr(await jget(url));});
document.getElementById('timeline-search').addEventListener('input',(ev)=>{timelineState.search=String((ev&&ev.target&&ev.target.value)||'');renderTimeline();});
document.getElementById('timeline-event-filter').addEventListener('change',(ev)=>{timelineState.eventType=String((ev&&ev.target&&ev.target.value)||'');renderTimeline();});
document.getElementById('timeline-group').addEventListener('change',(ev)=>{timelineState.group=String((ev&&ev.target&&ev.target.value)||'claim');renderTimeline();});
document.getElementById('conflicts-search').addEventListener('input',(ev)=>{conflictState.search=String((ev&&ev.target&&ev.target.value)||'');renderConflicts();});
document.getElementById('conflicts-include-stale').addEventListener('change',refreshConflicts);
document.getElementById('conflicts-refresh').addEventListener('click',refreshConflicts);
jget('/api/claims?limit=50').then(fillClaims).catch(()=>{});jget('/api/timeline?limit=40').then(fillTimeline).catch(()=>{});refreshConflicts().catch(()=>{});refreshQueue().catch(()=>{});jget('/api/audit?limit=40').then(fillAudit).catch(()=>{});jget('/api/namespaces?limit=200').then(fillNs).catch(()=>{});jget('/api/session-stats?limit=2000').then(fillStats).catch(()=>{});jget('/api/operator/status').then(fillOp).catch(()=>{});refreshObs().catch(()=>{});
const sb=document.getElementById('stream');const es=new EventSource('/api/operator/stream?last=20'); const append=(t)=>{const ex=sb.textContent.trim();sb.textContent=(ex&&ex!=='Waiting for operator to start...'?ex+'\\n':'')+t;}; ['message','stream_start','state_loaded','state_error','state_saved','json_error','turn_processed','reconcile_run','stream_exit'].forEach(n=>es.addEventListener(n,(ev)=>append(ev.data))); es.onerror=()=>append('[stream reconnecting]');
</script></main></body></html>"""
        body = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _triage_flags(self, limit: int) -> dict[int, dict[str, bool]]:
        flags: dict[int, dict[str, bool]] = {}
        for event in reversed(self._server.service.list_events(limit=limit, event_type="audit")):
            if event.claim_id is None:
                continue
            ref = flags.setdefault(int(event.claim_id), {"reviewed": False, "suppressed": False})
            details = str(event.details or "")
            if details == "triage_mark_reviewed":
                ref["reviewed"] = True
            if details == "triage_suppress":
                ref["suppressed"] = True
            if details == "triage_unsuppress":
                ref["suppressed"] = False
        return flags

    def _handle_claims(self, query_string: str) -> None:
        query = parse_qs(query_string)
        limit = _parse_int(_first_query_value(query, "limit"), default=50, minimum=1, maximum=500)
        include_archived = _parse_bool(_first_query_value(query, "include_archived"), default=False)
        allow_sensitive = _parse_bool(_first_query_value(query, "allow_sensitive"), default=False)
        status = _first_query_value(query, "status")
        claims = self._server.service.list_claims(status=status, limit=limit, include_archived=include_archived)
        if not allow_sensitive:
            claims = [claim for claim in claims if not is_sensitive_claim(claim)]
        self._write_json({"ok": True, "rows": len(claims), "claims": [_claim_to_dict(c) for c in claims]})

    def _handle_events(self, query_string: str) -> None:
        query = parse_qs(query_string)
        limit = _parse_int(_first_query_value(query, "limit"), default=100, minimum=1, maximum=1000)
        claim_id_raw = _first_query_value(query, "claim_id")
        event_type = _first_query_value(query, "event_type")
        claim_id = int(claim_id_raw) if claim_id_raw is not None else None
        if claim_id is not None and claim_id <= 0:
            raise ValueError("claim_id must be positive")
        events = self._server.service.list_events(claim_id=claim_id, limit=limit, event_type=event_type)
        self._write_json({"ok": True, "rows": len(events), "events": [_event_to_dict(e) for e in events]})

    def _handle_timeline(self, query_string: str) -> None:
        query = parse_qs(query_string)
        limit = _parse_int(_first_query_value(query, "limit"), default=100, minimum=1, maximum=2000)
        event_type = _first_query_value(query, "event_type")
        events = self._server.service.list_events(limit=limit, event_type=event_type)
        self._write_json({"ok": True, "rows": len(events), "timeline": [_event_to_dict(e) for e in events]})

    def _handle_conflicts(self, query_string: str) -> None:
        query = parse_qs(query_string)
        limit = _parse_int(_first_query_value(query, "limit"), default=50, minimum=1, maximum=500)
        include_stale = _parse_bool(_first_query_value(query, "include_stale"), default=False)
        conflicted = self._server.service.list_claims(status="conflicted", limit=limit, include_archived=False)
        if not conflicted:
            self._write_json({"ok": True, "rows": 0, "groups": []})
            return
        statuses = ["confirmed", "conflicted"] + (["stale"] if include_stale else [])
        active = self._server.service.store.list_claims(limit=max(limit * 12, 200), status_in=statuses, include_archived=False, include_citations=True)
        grouped: dict[tuple[str, str, str], list[Any]] = {}
        for claim in conflicted:
            grouped.setdefault((str(claim.subject or ""), str(claim.predicate or ""), str(claim.scope or "project")), [])
        for claim in active:
            key = (str(claim.subject or ""), str(claim.predicate or ""), str(claim.scope or "project"))
            if key in grouped:
                grouped[key].append(claim)
        groups_payload: list[dict[str, Any]] = []
        for key, claims in grouped.items():
            claims_sorted = sorted(claims, key=lambda c: (str(c.updated_at), int(c.id)), reverse=True)
            groups_payload.append({"subject": key[0], "predicate": key[1], "scope": key[2], "claims": [_claim_to_dict(c) for c in claims_sorted]})
        groups_payload.sort(key=lambda g: (g["subject"], g["predicate"], g["scope"]))
        self._write_json({"ok": True, "rows": len(groups_payload), "groups": groups_payload})

    def _handle_review_queue(self, query_string: str) -> None:
        query = parse_qs(query_string)
        limit = _parse_int(_first_query_value(query, "limit"), default=100, minimum=1, maximum=1000)
        include_stale = _parse_bool(_first_query_value(query, "include_stale"), default=True)
        include_conflicted = _parse_bool(_first_query_value(query, "include_conflicted"), default=True)
        allow_sensitive = _parse_bool(_first_query_value(query, "allow_sensitive"), default=False)
        exclude_reviewed = _parse_bool(_first_query_value(query, "exclude_reviewed"), default=False)
        exclude_suppressed = _parse_bool(_first_query_value(query, "exclude_suppressed"), default=False)
        items = build_review_queue(
            self._server.service,
            limit=limit,
            include_stale=include_stale,
            include_conflicted=include_conflicted,
            include_sensitive=allow_sensitive,
        )
        flags = self._triage_flags(max(limit * 20, 200))
        queue = queue_to_dicts(items)
        out: list[dict[str, Any]] = []
        for item in queue:
            claim_id = int(item["claim_id"])
            triage = flags.get(claim_id, {"reviewed": False, "suppressed": False})
            item["reviewed"] = bool(triage["reviewed"])
            item["suppressed"] = bool(triage["suppressed"])
            if exclude_reviewed and bool(item["reviewed"]):
                continue
            if exclude_suppressed and bool(item["suppressed"]):
                continue
            out.append(item)
        self._write_json({"ok": True, "rows": len(out), "items": out})

    def _handle_retrieval(self, query_string: str) -> None:
        query = parse_qs(query_string)
        text = _first_query_value(query, "query") or ""
        mode = (_first_query_value(query, "mode") or "hybrid").strip().lower()
        mode = mode if mode in {"legacy", "hybrid"} else "hybrid"
        limit = _parse_int(_first_query_value(query, "limit"), default=10, minimum=1, maximum=100)
        include_stale = _parse_bool(_first_query_value(query, "include_stale"), default=True)
        include_conflicted = _parse_bool(_first_query_value(query, "include_conflicted"), default=True)
        allow_sensitive = _parse_bool(_first_query_value(query, "allow_sensitive"), default=False)
        scope_raw = _first_query_value(query, "scope_allowlist")
        scope_allowlist: list[str] = []
        if scope_raw:
            dedupe: set[str] = set()
            for token in scope_raw.split(","):
                normalized = token.strip()
                if normalized and normalized not in dedupe:
                    dedupe.add(normalized)
                    scope_allowlist.append(normalized)

        def _call_retrieval_method(method: Any, *, with_scope: bool) -> Any:
            kwargs: dict[str, Any] = {
                "limit": limit,
                "include_stale": include_stale,
                "include_conflicted": include_conflicted,
                "retrieval_mode": mode,
                "allow_sensitive": allow_sensitive,
            }
            if with_scope and scope_allowlist:
                kwargs["scope_allowlist"] = scope_allowlist
            attempts: list[dict[str, Any]] = [kwargs]
            if "scope_allowlist" in kwargs:
                fallback = dict(kwargs)
                fallback.pop("scope_allowlist", None)
                attempts.append(fallback)
            if "allow_sensitive" in kwargs:
                fallback = dict(attempts[-1])
                fallback.pop("allow_sensitive", None)
                attempts.append(fallback)
            last_error: TypeError | None = None
            for attempt in attempts:
                try:
                    return method(text, **attempt)
                except TypeError as exc:
                    last_error = exc
                    if "unexpected keyword argument" not in str(exc):
                        raise
            if last_error is not None:
                raise last_error
            return []

        query_rows_fn = getattr(self._server.service, "query_rows", None)
        claims: list[Any] = []
        scored_by_claim_id: dict[int, dict[str, Any]] = {}
        if callable(query_rows_fn):
            rows_data = _call_retrieval_method(query_rows_fn, with_scope=True)
            if isinstance(rows_data, list):
                for row in rows_data:
                    claim_obj = None
                    if isinstance(row, dict):
                        claim_obj = row.get("claim")
                    else:
                        claim_obj = getattr(row, "claim", None)
                    if claim_obj is None:
                        continue
                    try:
                        claim_id = int(claim_obj.id)
                    except Exception:
                        continue
                    claims.append(claim_obj)
                    if isinstance(row, dict):
                        scored_by_claim_id[claim_id] = row
                    else:
                        scored_by_claim_id[claim_id] = {
                            "score": getattr(row, "score", None),
                            "lexical_score": getattr(row, "lexical_score", None),
                            "confidence_score": getattr(row, "confidence_score", None),
                            "freshness_score": getattr(row, "freshness_score", None),
                            "vector_score": getattr(row, "vector_score", None),
                        }

        if not claims:
            claims = _call_retrieval_method(self._server.service.query, with_scope=True)
        triage_flags = self._triage_flags(max(limit * 30, 200))
        q_tokens = {token for token in text.lower().split() if token.strip()}
        rows: list[dict[str, Any]] = []
        for claim in claims:
            claim_id = int(claim.id)
            c_tokens = set(str(claim.text or "").lower().split())
            lexical = (len(q_tokens & c_tokens) / max(1, len(q_tokens))) if q_tokens else 0.0
            confidence = max(0.0, min(1.0, float(claim.confidence)))
            freshness = 0.5
            vector = 0.0
            _w_l, _w_c, _w_f = get_config().retrieval_weights_no_vector
            score = (_w_l * lexical) + (_w_c * confidence) + (_w_f * freshness)
            scored = scored_by_claim_id.get(claim_id)
            if isinstance(scored, dict):
                lexical_raw = scored.get("lexical_score", scored.get("lexical", lexical))
                confidence_raw = scored.get("confidence_score", scored.get("confidence", confidence))
                freshness_raw = scored.get("freshness_score", scored.get("freshness", freshness))
                vector_raw = scored.get("vector_score", scored.get("vector", vector))
                score_raw = scored.get("score", score)
                with contextlib.suppress(TypeError, ValueError):
                    lexical = float(lexical_raw)
                with contextlib.suppress(TypeError, ValueError):
                    confidence = float(confidence_raw)
                with contextlib.suppress(TypeError, ValueError):
                    freshness = float(freshness_raw)
                with contextlib.suppress(TypeError, ValueError):
                    vector = float(vector_raw)
                with contextlib.suppress(TypeError, ValueError):
                    score = float(score_raw)
            triage = triage_flags.get(claim_id, {"reviewed": False, "suppressed": False})
            annotation_parts: list[str] = []
            normalized_status = str(claim.status or "").strip().lower()
            if normalized_status == "stale":
                annotation_parts.append("stale: refresh or re-validate")
            if normalized_status == "conflicted":
                annotation_parts.append("conflicted: compare competing values")
            if bool(triage["reviewed"]):
                annotation_parts.append("triage reviewed")
            if bool(triage["suppressed"]):
                annotation_parts.append("triage suppressed")
            if bool(getattr(claim, "pinned", False)):
                annotation_parts.append("pinned")
            rows.append(
                {
                    "claim": _claim_to_dict(claim),
                    "status": str(claim.status or ""),
                    "annotation": ", ".join(annotation_parts) if annotation_parts else "active",
                    "triage_reviewed": bool(triage["reviewed"]),
                    "triage_suppressed": bool(triage["suppressed"]),
                    "score": float(score),
                    "lexical_score": float(lexical),
                    "confidence_score": float(confidence),
                    "freshness_score": float(freshness),
                    "vector_score": float(vector),
                }
            )
        self._write_json({"ok": True, "rows": len(rows), "rows_data": rows, "query": text, "mode": mode, "scope_allowlist": scope_allowlist})

    def _handle_audit(self, query_string: str) -> None:
        query = parse_qs(query_string)
        limit = _parse_int(_first_query_value(query, "limit"), default=50, minimum=1, maximum=1000)
        rows: list[dict[str, Any]] = []
        for event_type in ("audit", "policy_decision"):
            events = self._server.service.list_events(limit=limit, event_type=event_type)
            rows.extend(_event_to_dict(event) for event in events)
        rows.sort(key=lambda e: (str(e.get("created_at", "")), int(e.get("id", 0))), reverse=True)
        self._write_json({"ok": True, "rows": min(limit, len(rows)), "events": rows[:limit]})

    def _handle_namespaces(self, query_string: str) -> None:
        query = parse_qs(query_string)
        limit = _parse_int(_first_query_value(query, "limit"), default=200, minimum=1, maximum=5000)
        claims = self._server.service.list_claims(limit=limit, include_archived=False)
        buckets: dict[str, list[Any]] = {"facts": [], "decisions": [], "workflows": [], "project_overview": []}
        for claim in claims:
            text = str(claim.text or "").lower()
            claim_type = str(claim.claim_type or "").lower()
            predicate = str(claim.predicate or "").lower()
            is_decision = ("decision" in claim_type) or (predicate in {"decision", "policy"}) or ("decid" in text)
            is_workflow = any(k in claim_type for k in {"workflow", "runbook", "process"}) or predicate in {"workflow", "runbook", "command", "step"}
            if is_decision:
                buckets["decisions"].append(claim)
            elif is_workflow:
                buckets["workflows"].append(claim)
            else:
                buckets["facts"].append(claim)
            buckets["project_overview"].append(claim)

        def _samples(rows: list[Any]) -> list[dict[str, Any]]:
            return [{"id": int(c.id), "subject": c.subject, "predicate": c.predicate, "object_value": c.object_value, "status": c.status} for c in rows[:5]]

        self._write_json(
            {
                "ok": True,
                "rows": len(claims),
                "namespaces": {
                    "facts": {"count": len(buckets["facts"]), "samples": _samples(buckets["facts"])},
                    "decisions": {"count": len(buckets["decisions"]), "samples": _samples(buckets["decisions"])},
                    "workflows": {"count": len(buckets["workflows"]), "samples": _samples(buckets["workflows"])},
                    "project_overview": {"count": len(buckets["project_overview"]), "samples": _samples(buckets["project_overview"])},
                },
            }
        )

    def _handle_session_stats(self, query_string: str) -> None:
        query = parse_qs(query_string)
        limit = _parse_int(_first_query_value(query, "limit"), default=2000, minimum=1, maximum=20000)
        rows = _tail_events_from_jsonl(self._server.operator_log_jsonl, limit)
        event_counts: Counter[str] = Counter()
        tool_counts: Counter[str] = Counter()
        sessions: set[str] = set()
        threads: set[str] = set()
        for row in rows:
            event_counts[str(row.get("event") or "message")] += 1
            session = str(row.get("session_id") or "").strip()
            thread = str(row.get("thread_id") or "").strip()
            if session:
                sessions.add(session)
            if thread:
                threads.add(thread)
            tool = str(row.get("tool") or row.get("tool_name") or "").strip()
            if tool:
                tool_counts[tool] += 1
        latency_stats = _latency_summary_from_rows(rows)
        self._write_json({"ok": True, "summary": {"rows_scanned": len(rows), "sessions": len(sessions), "threads": len(threads), "event_counts": dict(event_counts), "tool_counts": dict(tool_counts), "latency_ms": latency_stats}})

    def _handle_observability(self, query_string: str) -> None:
        query = parse_qs(query_string)
        log_limit = _parse_int(_first_query_value(query, "log_limit"), default=1500, minimum=1, maximum=20000)
        event_limit = _parse_int(_first_query_value(query, "event_limit"), default=600, minimum=1, maximum=10000)
        queue_limit = _parse_int(_first_query_value(query, "queue_limit"), default=250, minimum=1, maximum=2000)

        log_rows = _tail_events_from_jsonl(self._server.operator_log_jsonl, log_limit)
        log_event_counts: Counter[str] = Counter()
        tool_counts: Counter[str] = Counter()
        sessions: set[str] = set()
        threads: set[str] = set()
        for row in log_rows:
            log_event_counts[str(row.get("event") or "message")] += 1
            session = str(row.get("session_id") or "").strip()
            thread = str(row.get("thread_id") or "").strip()
            if session:
                sessions.add(session)
            if thread:
                threads.add(thread)
            tool = str(row.get("tool") or row.get("tool_name") or "").strip()
            if tool:
                tool_counts[tool] += 1

        recent_events = self._server.service.list_events(limit=event_limit)
        event_counts: Counter[str] = Counter(str(event.event_type or "event") for event in recent_events)

        queue_items = build_review_queue(
            self._server.service,
            limit=queue_limit,
            include_stale=True,
            include_conflicted=True,
            include_sensitive=False,
        )
        triage_flags = self._triage_flags(max(queue_limit * 20, 200))
        status_counts: Counter[str] = Counter()
        triage_reviewed = 0
        triage_suppressed = 0
        actionable = 0
        for item in queue_items:
            status_counts[str(item.status or "unknown")] += 1
            triage = triage_flags.get(int(item.claim_id), {"reviewed": False, "suppressed": False})
            reviewed = bool(triage["reviewed"])
            suppressed = bool(triage["suppressed"])
            if reviewed:
                triage_reviewed += 1
            if suppressed:
                triage_suppressed += 1
            if not reviewed and not suppressed:
                actionable += 1

        top_queue = []
        for item in queue_items[:5]:
            triage = triage_flags.get(int(item.claim_id), {"reviewed": False, "suppressed": False})
            top_queue.append(
                {
                    "claim_id": int(item.claim_id),
                    "status": str(item.status or "unknown"),
                    "priority": float(item.priority),
                    "reason": str(item.reason or ""),
                    "reviewed": bool(triage["reviewed"]),
                    "suppressed": bool(triage["suppressed"]),
                }
            )

        self._write_json(
            {
                "ok": True,
                "observability": {
                    "operator": {
                        **self._server.operator_status(),
                        "rows_scanned": len(log_rows),
                        "sessions": len(sessions),
                        "threads": len(threads),
                        "event_counts": dict(log_event_counts),
                        "tool_counts": dict(tool_counts),
                        "latency_ms": _latency_summary_from_rows(log_rows),
                    },
                    "events_recent": {
                        "rows_scanned": len(recent_events),
                        "event_counts": dict(event_counts),
                    },
                    "queue": {
                        "rows_scanned": len(queue_items),
                        "status_counts": dict(status_counts),
                        "actionable": actionable,
                        "triage_reviewed": triage_reviewed,
                        "triage_suppressed": triage_suppressed,
                        "top": top_queue,
                    },
                },
            }
        )

    def _handle_triage_action(self, payload: dict[str, Any]) -> None:
        action = str(payload.get("action") or "").strip().lower()
        claim_id = int(payload.get("claim_id", 0))
        if claim_id <= 0:
            raise ValueError("claim_id must be positive")
        if action not in {"pin", "unpin", "mark_reviewed", "suppress", "unsuppress", "approve_proposal", "reject_proposal"}:
            raise ValueError("unsupported action")
        if action == "pin":
            self._write_json({"ok": True, "action": action, "claim": _claim_to_dict(self._server.service.pin(claim_id, pin=True))})
            return
        if action == "unpin":
            self._write_json({"ok": True, "action": action, "claim": _claim_to_dict(self._server.service.pin(claim_id, pin=False))})
            return
        if action in {"approve_proposal", "reject_proposal"}:
            from memorymaster.steward import resolve_steward_proposal

            resolved = resolve_steward_proposal(
                self._server.service,
                action=("approve" if action == "approve_proposal" else "reject"),
                claim_id=claim_id,
                apply_on_approve=True,
            )
            self._write_json({"ok": True, "action": action, "result": resolved})
            return
        detail_map = {"mark_reviewed": "triage_mark_reviewed", "suppress": "triage_suppress", "unsuppress": "triage_unsuppress"}
        self._server.service.store.record_event(claim_id=claim_id, event_type="audit", details=detail_map[action], payload={"source": "dashboard"})
        self._write_json({"ok": True, "action": action, "claim_id": claim_id})

    def _handle_operator_control(self, payload: dict[str, Any]) -> None:
        action = str(payload.get("action") or "").strip().lower()
        if action == "start":
            inbox_jsonl = str(payload.get("inbox_jsonl") or "artifacts/operator/operator_inbox.jsonl")
            self._write_json({"ok": True, **self._server.start_operator(inbox_jsonl)})
            return
        if action == "stop":
            self._write_json({"ok": True, **self._server.stop_operator()})
            return
        raise ValueError("action must be start or stop")

    def _handle_operator_stream(self, query_string: str) -> None:
        query = parse_qs(query_string)
        last = _parse_int(_first_query_value(query, "last"), default=20, minimum=0, maximum=2000)
        follow = _parse_bool(_first_query_value(query, "follow"), default=True)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        def send(record: dict[str, Any]) -> bool:
            chunk = f"event: {str(record.get('event') or 'message')}\ndata: {json.dumps(record, ensure_ascii=True)}\n\n".encode("utf-8")
            try:
                self.wfile.write(chunk)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                return False
            return True

        log_path = self._server.operator_log_jsonl
        for event in _tail_events_from_jsonl(log_path, last):
            if not send(event):
                return
        if not follow:
            return
        offset = log_path.stat().st_size if log_path.exists() else 0
        while True:
            if log_path.exists():
                with log_path.open("r", encoding="utf-8") as handle:
                    handle.seek(offset)
                    while True:
                        raw = handle.readline()
                        if not raw:
                            break
                        offset = handle.tell()
                        line = raw.strip().lstrip("\ufeff")
                        if not line:
                            continue
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(record, dict) and not send(record):
                            return
            time.sleep(0.25)


def create_dashboard_server(
    *,
    db_target: str | Path | None = None,
    service: MemoryService | None = None,
    workspace_root: str | Path | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
    operator_log_jsonl: str | Path = "artifacts/operator/operator_events.jsonl",
) -> DashboardHTTPServer:
    if service is None:
        if db_target is None:
            db_target = "memorymaster.db"
        service = MemoryService(db_target, workspace_root=workspace_root)
    else:
        if db_target is None:
            db_target = getattr(service.store, "db_path", None) or "memorymaster.db"
    return DashboardHTTPServer((host, int(port)), service=service, operator_log_jsonl=operator_log_jsonl, db_target=db_target, workspace_root=workspace_root)


def run_dashboard(
    *,
    db_target: str | Path = "memorymaster.db",
    workspace_root: str | Path = ".",
    host: str = "127.0.0.1",
    port: int = 8765,
    operator_log_jsonl: str | Path = "artifacts/operator/operator_events.jsonl",
) -> None:
    server = create_dashboard_server(db_target=db_target, workspace_root=workspace_root, host=host, port=port, operator_log_jsonl=operator_log_jsonl)
    print(f"memorymaster dashboard listening on http://{host}:{port}/dashboard")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run MemoryMaster dashboard")
    parser.add_argument("--db", default="memorymaster.db", help="SQLite path or Postgres DSN")
    parser.add_argument("--workspace", default=".", help="Workspace root")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host")
    parser.add_argument("--port", type=int, default=8765, help="Bind port")
    parser.add_argument("--operator-log-jsonl", default="artifacts/operator/operator_events.jsonl", help="Operator events path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    run_dashboard(db_target=args.db, workspace_root=args.workspace, host=args.host, port=args.port, operator_log_jsonl=args.operator_log_jsonl)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Dashboard 'Decisions' tab: Jev health per surface and the weekly operator review queue.

Routes (wired in ``dashboard.py``, behind the dashboard's existing auth):

* ``GET /decisions`` - the page;
* ``GET /api/decisions/metrics?days=N`` - ``decisions.metrics.compute_metrics`` per
  surface plus configured modes, daily cap and today's spend (viewer role);
* ``GET /api/decisions/review-queue?days=N&size=M`` - about 20 decisions chosen for
  information (viewer role);
* ``POST /api/decisions/review`` - Correct/Incorrect label, recorded as an
  ``operator`` outcome (operator role + same-origin check, like every dashboard write).

Reads use a physically read-only view of the ledger; an absent ledger reads as
empty and is never created, an unreadable one is a 503 rather than zeros.
"""
from __future__ import annotations

from http import HTTPStatus
from typing import Any
from urllib.parse import parse_qs

from memorymaster.decisions.ledger import DecisionLedger, LedgerReadError
from memorymaster.surfaces import dashboard_template
from memorymaster.surfaces.jev_review import (
    REVIEW_DAYS,
    REVIEW_SIZE,
    default_ledger_path,
    metrics_payload,
    read_ledger,
    record_review_label,
    select_review_queue,
)

MAX_DAYS = 90
MAX_SIZE = 100

_PAGE_CSS = """
.jev-nav a[aria-current="page"]{color:#f8fafc;font-weight:600;text-decoration:none}
.jev-bins td,.jev-bins th{padding:4px 6px}
.jev-bar{display:inline-block;height:8px;border-radius:4px;background:#3b82f6;vertical-align:middle}
.jev-card{border:1px solid #334155;border-radius:8px;margin:8px 0;padding:10px;background:#0f172a}
.jev-card.done{opacity:.6}
.jev-card .row{margin:4px 0}
.jev-card pre{white-space:pre-wrap;word-break:break-word;margin:4px 0;font-size:.78rem;color:#cbd5e1}
.badge-reason{background:#312e81;color:#c7d2fe;border:1px solid #4f46e5;margin-right:4px}
.badge-warn{background:#451a03;color:#fdba74;border:1px solid #d97706}
.badge-bad{background:#450a0a;color:#fca5a5;border:1px solid #dc2626}
.badge-ok{background:#14532d;color:#86efac;border:1px solid #16a34a}
.jev-actions{display:flex;gap:8px;margin-top:8px;flex-wrap:wrap}
.scroll table.jev-wide{min-width:1040px}
"""

_BODY = """<main>
<div class="header">
<div class="logo">M</div>
<div><h1>Jev decisions</h1><div class="subtitle">What Jev decided, when it fell back, and whether it was right</div></div>
<span class="version">v__MEMORYMASTER_VERSION__</span>
</div>
<nav class="toolbar jev-nav" aria-label="Dashboard navigation"><a href="/dashboard">Dashboard</a> <a href="/decisions" aria-current="page">Jev decisions</a> <a href="/review">Review memory usefulness</a>
<label class="muted" for="jev-days">Window</label> <select id="jev-days" aria-label="Metrics window"><option value="1">24 hours</option><option value="7" selected>7 days</option><option value="30">30 days</option></select> <button id="jev-refresh" type="button">Refresh</button></nav>
<div id="jev-status" class="muted" role="status" aria-live="polite">Loading...</div>
<div class="grid">
<section class="wide">
<div class="section-head"><div class="icon icon-blue">&#9881;</div><div><h2>Surfaces</h2><div class="desc">Volume, live share, fallbacks by reason, transport and whole-engine latency, breaker opens, blocked egress</div></div></div>
<div class="scroll"><table class="jev-wide"><thead><tr><th>Surface</th><th>Configured</th><th>Decisions</th><th>Live</th><th>Fallbacks</th><th>Transport p50/p95/p99</th><th>Engine p50/p95/p99</th><th>Breaker opens</th><th>Egress blocked</th><th>Agrees w/ legacy</th></tr></thead><tbody id="jev-surfaces"><tr><td colspan="10" class="empty">Loading...</td></tr></tbody></table></div>
</section>
<section>
<div class="section-head"><div class="icon icon-amber">&#36;</div><div><h2>Cost per day</h2><div class="desc">Spend summed from the ledger against the daily cap (UTC days)</div></div></div>
<div class="scroll"><table><thead><tr><th>Day</th><th>Cost</th><th>Cap</th><th>Status</th></tr></thead><tbody id="jev-cost"><tr><td colspan="4" class="empty">Loading...</td></tr></tbody></table></div>
</section>
<section>
<div class="section-head"><div class="icon icon-green">&#8594;</div><div><h2>Exposure to use</h2><div class="desc">Exposed items later used in the turn, by arm (Jev policy, legacy, explored)</div></div></div>
<div class="scroll"><table><thead><tr><th>Surface</th><th>Arm</th><th>Exposed</th><th>Used</th><th>Rate</th></tr></thead><tbody id="jev-exposure"><tr><td colspan="5" class="empty">Loading...</td></tr></tbody></table></div>
</section>
<section class="wide">
<div class="section-head"><div class="icon icon-purple">&#127919;</div><div><h2>Calibration</h2><div class="desc">Reliability bins, ECE and Brier per question version where outcomes exist</div></div></div>
<div id="jev-calibration"><div class="empty">Loading...</div></div>
</section>
<section class="wide">
<div class="section-head"><div class="icon icon-cyan">&#8776;</div><div><h2>Drift</h2><div class="desc">Weekly population stability index of answer distributions (0.1 moderate, 0.25 shift)</div></div></div>
<div id="jev-drift"><div class="empty">Loading...</div></div>
</section>
<section class="wide">
<div class="section-head"><div class="icon icon-pink">&#10003;</div><div><h2>Weekly review queue</h2><div class="desc">Decisions chosen for what a label teaches: near a threshold, Jev vs legacy or steward disagreement, paraphrase disagreement, one per score decile</div></div></div>
<div id="jev-review-status" class="muted" role="status" aria-live="polite"></div>
<div id="jev-review-queue"><div class="empty">Loading...</div></div>
</section>
</div>
</main>"""

_SCRIPT = r"""
const esc=(v)=>String(v==null?'':v).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'",'&#39;');
const pct=(v)=>typeof v==='number'?(v*100).toFixed(1)+'%':'-';
const ms=(v)=>typeof v==='number'?Math.round(v)+' ms':'-';
const num=(v,d)=>typeof v==='number'?v.toFixed(d==null?3:d):'-';
const usd=(v)=>typeof v==='number'?'$'+v.toFixed(4):'-';
const pretty=(v)=>esc(JSON.stringify(v==null?null:v,null,2));
const queueState={items:[]};
async function jget(p){const r=await fetch(p);const d=await r.json();if(!r.ok||d.ok===false)throw new Error((d&&d.error)||('HTTP '+r.status));return d;}
async function jpost(p,b){const r=await fetch(p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})});const d=await r.json();if(!r.ok||d.ok===false)throw new Error((d&&d.error)||('HTTP '+r.status));return d;}
function empty(id,cols,text){const node=document.getElementById(id);if(cols){node.innerHTML='<tr><td colspan="'+cols+'" class="empty">'+esc(text)+'</td></tr>';}else{node.innerHTML='<div class="empty">'+esc(text)+'</div>';}}
function triple(x){x=x||{};return ms(x.p50)+' / '+ms(x.p95)+' / '+ms(x.p99)+' <span class="muted">n='+esc(x.n||0)+'</span>';}
function pills(obj){const rows=Object.entries(obj||{}).sort((a,b)=>Number(b[1])-Number(a[1]));return rows.length?rows.map(([k,v])=>'<span class="mono">'+esc(k)+'='+esc(v)+'</span>').join(' '):'<span class="muted">none</span>';}
function modeBadge(m){const cls=m==='live'?'badge-ok':(m==='shadow'?'badge-warn':'');return '<span class="badge '+cls+'">'+esc(m||'-')+'</span>';}
function fillSurfaces(d){const modes=d.configured_modes||{};const surfaces=d.surfaces||{};const names=[...new Set([...Object.keys(modes),...Object.keys(surfaces)])].sort();if(!names.length){empty('jev-surfaces',10,'No surfaces configured');return;}document.getElementById('jev-surfaces').innerHTML=names.map(n=>{const s=surfaces[n];if(!s){return '<tr><td class="mono">'+esc(n)+'</td><td>'+modeBadge(modes[n])+'</td><td colspan="8" class="muted">no decisions in this window</td></tr>';}return '<tr><td class="mono">'+esc(n)+'</td><td>'+modeBadge(modes[n])+'</td><td class="mono">'+esc(s.volume)+'</td><td class="mono">'+pct(s.live_pct)+'</td><td class="wrap">'+pills(s.fallback_by_reason)+'</td><td class="mono wrap">'+triple(s.latency_ms)+'</td><td class="mono wrap">'+triple(s.engine_ms)+'</td><td class="mono">'+esc(s.breaker_opens)+'</td><td class="mono">'+esc(s.egress_blocked)+'</td><td class="mono">'+pct(s.agreement_with_legacy)+'</td></tr>';}).join('');}
function fillCost(d){const rows=Array.isArray(d.cost_per_day)?d.cost_per_day:[];if(!rows.length){empty('jev-cost',4,'No spend in this window');return;}document.getElementById('jev-cost').innerHTML=rows.slice().reverse().map(r=>'<tr><td class="mono">'+esc(r.day)+'</td><td class="mono">'+usd(r.cost_usd)+'</td><td class="mono">'+usd(r.cap)+'</td><td>'+(r.over_cap?'<span class="badge badge-bad">over cap</span>':'<span class="badge badge-ok">within</span>')+'</td></tr>').join('');}
function fillExposure(d){const rows=[];Object.entries(d.surfaces||{}).sort().forEach(([n,s])=>{Object.entries(s.exposure_use||{}).sort().forEach(([arm,x])=>rows.push('<tr><td class="mono">'+esc(n)+'</td><td>'+esc(arm)+'</td><td class="mono">'+esc(x.exposed)+'</td><td class="mono">'+esc(x.used)+'</td><td class="mono">'+pct(x.rate)+'</td></tr>'));});if(!rows.length){empty('jev-exposure',5,'No exposed items in this window');return;}document.getElementById('jev-exposure').innerHTML=rows.join('');}
function fillCalibration(d){const entries=Object.entries(d.calibration||{}).sort();const box=document.getElementById('jev-calibration');if(!entries.length){box.innerHTML='<div class="empty">No matured outcomes yet: calibration needs exposed items with observed outcomes</div>';return;}box.innerHTML=entries.map(([key,c])=>{const bins=(c.bins||[]).filter(b=>b.n).map(b=>'<tr><td class="mono">'+num(b.lower,1)+'-'+num(b.upper,1)+'</td><td class="mono">'+esc(b.n)+'</td><td class="mono">'+num(b.confidence)+'</td><td class="mono">'+num(b.accuracy)+'</td><td><span class="jev-bar" style="width:'+Math.round(100*(b.accuracy||0))+'px"></span></td></tr>').join('');return '<div class="jev-card"><div class="row"><strong class="mono">'+esc(key)+'</strong> <span class="muted">outcome '+esc(c.positive_kind)+'</span></div><div class="stat-row"><div class="stat-item"><span class="stat-value">'+esc(c.n)+'</span><span class="stat-label">Items</span></div><div class="stat-item"><span class="stat-value">'+pct(c.base_rate)+'</span><span class="stat-label">Base rate</span></div><div class="stat-item"><span class="stat-value">'+num(c.ece)+'</span><span class="stat-label">ECE</span></div><div class="stat-item"><span class="stat-value">'+num(c.brier)+'</span><span class="stat-label">Brier</span></div></div><details><summary>Reliability bins</summary><table class="jev-bins"><thead><tr><th>Bin</th><th>n</th><th>Mean p</th><th>Observed</th><th></th></tr></thead><tbody>'+bins+'</tbody></table></details></div>';}).join('');}
function fillDrift(d){const entries=Object.entries(d.drift||{}).sort();const box=document.getElementById('jev-drift');if(!entries.length){box.innerHTML='<div class="empty">No answers in the current week</div>';return;}box.innerHTML='<table><thead><tr><th>Question</th><th>PSI vs previous week</th><th>Status</th></tr></thead><tbody>'+entries.map(([k,v])=>{const flag=typeof v!=='number'?'<span class="muted">no previous week</span>':(v>=0.25?'<span class="badge badge-bad">shift</span>':(v>=0.1?'<span class="badge badge-warn">moderate</span>':'<span class="badge badge-ok">stable</span>'));return '<tr><td class="mono">'+esc(k)+'</td><td class="mono">'+num(v)+'</td><td>'+flag+'</td></tr>';}).join('')+'</tbody></table>';}
function fillMetrics(d){const cap=d.daily_usd_cap;const parts=['Window '+esc(d.days)+' days','today '+usd(d.cost_today_usd)+' of '+usd(cap)+' cap'];if(d.ledger_write_failures){parts.push(esc(d.ledger_write_failures)+' ledger write failures in this process');}if(!d.ledger_present){parts.push('no decisions ledger yet: Jev is off or has not decided anything');}const status=document.getElementById('jev-status');status.className='muted';status.textContent=parts.join(' | ');fillSurfaces(d);fillCost(d);fillExposure(d);fillCalibration(d);fillDrift(d);}
function reasonBadges(e){return (e.reasons||[]).map(r=>'<span class="badge badge-reason">'+esc(r.replaceAll('_',' '))+'</span>').join('');}
function fillQueue(d){queueState.items=Array.isArray(d.items)?d.items:[];const box=document.getElementById('jev-review-queue');if(!queueState.items.length){box.innerHTML='<div class="empty">Nothing to review this week</div>';return;}box.innerHTML=queueState.items.map((e,i)=>{const answer=typeof e.answer==='number'?num(e.answer,2):esc(e.answer);const thresholds=Object.keys(e.thresholds||{}).length?Object.entries(e.thresholds).map(([k,v])=>esc(k)+' '+num(v,2)).join(', '):'none';const para=e.paraphrase?'<div class="row"><strong>Paraphrase:</strong> <span class="mono">'+esc(e.paraphrase.question_id)+'</span> answered '+num(e.paraphrase.answer,2)+'</div>':'';const outcomes=(e.lifecycle_outcomes||[]).length?'<div class="row"><strong>Later:</strong> '+e.lifecycle_outcomes.map(esc).join(', ')+'</div>':'';return '<div class="jev-card" data-index="'+i+'"><div class="row">'+reasonBadges(e)+'<span class="badge">'+esc(e.surface)+'</span> <span class="mono muted">'+esc(e.ts)+'</span></div><div class="row"><strong>'+esc(e.question||e.question_id)+'</strong></div><div class="row"><span class="mono">'+esc(e.question_id)+'@v'+esc(e.question_version)+'</span> on <span class="mono">'+esc(e.item_ref||'(decision)')+'</span>: Jev answered <strong class="mono">'+answer+'</strong> <span class="muted">thresholds '+thresholds+'</span></div>'+para+outcomes+'<details><summary>What Jev saw (redacted) and what was done</summary><div class="row"><strong>Item:</strong><pre>'+pretty(e.subject)+'</pre></div><div class="row"><strong>Request:</strong><pre>'+pretty(e.state)+'</pre></div><div class="row"><strong>Legacy:</strong> <span class="mono">'+esc(JSON.stringify(e.legacy_action))+'</span> <strong>Jev:</strong> <span class="mono">'+esc(JSON.stringify(e.jev_action))+'</span> <strong>Taken:</strong> <span class="mono">'+esc(JSON.stringify(e.action_taken))+'</span> <span class="muted">'+esc(e.mode)+' / '+esc(e.exploration_arm||'-')+'</span></div></details><div class="jev-actions"><button type="button" class="primary" data-verdict="correct" data-index="'+i+'">Correct</button><button type="button" class="danger" data-verdict="incorrect" data-index="'+i+'">Incorrect</button></div></div>';}).join('');}
document.getElementById('jev-review-queue').addEventListener('click',async(ev)=>{const t=ev.target;if(!t||t.tagName!=='BUTTON'||!t.hasAttribute('data-verdict'))return;const e=queueState.items[Number(t.getAttribute('data-index'))];if(!e)return;const verdict=t.getAttribute('data-verdict');const card=t.closest('.jev-card');const status=document.getElementById('jev-review-status');const buttons=card?card.querySelectorAll('button'):[];buttons.forEach(b=>{b.disabled=true;});try{await jpost('/api/decisions/review',{decision_id:e.decision_id,item_ref:e.item_ref,question_id:e.question_id,verdict:verdict});if(card){card.classList.add('done');}status.className='muted';status.textContent='Recorded "'+verdict+'" for '+e.question_id+' on '+(e.item_ref||'the decision')+'.';}catch(error){buttons.forEach(b=>{b.disabled=false;});status.className='error-state';status.textContent='Label not recorded: '+String((error&&error.message)||error);}});
async function refresh(){const days=document.getElementById('jev-days').value||'7';const status=document.getElementById('jev-status');try{fillMetrics(await jget('/api/decisions/metrics?days='+encodeURIComponent(days)));}catch(error){status.className='error-state';status.textContent='Could not load decision metrics: '+String((error&&error.message)||error);}try{fillQueue(await jget('/api/decisions/review-queue?days=7'));}catch(error){empty('jev-review-queue',0,'Could not load the review queue: '+String((error&&error.message)||error));}}
document.getElementById('jev-refresh').addEventListener('click',refresh);document.getElementById('jev-days').addEventListener('change',refresh);refresh();
"""


def _base_css() -> str:
    html = dashboard_template.DASHBOARD_HTML
    start, end = html.find("<style>"), html.find("</style>")
    return html[start + len("<style>"):end] if 0 <= start < end else ""


def render_page(version: str) -> str:
    return (
        '<!doctype html>\n<html lang="en"><head>\n<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>Jev decisions - MemoryMaster</title>\n<style>{_base_css()}{_PAGE_CSS}</style></head><body>\n"
        + _BODY.replace("__MEMORYMASTER_VERSION__", version)
        + f"\n<script>{_SCRIPT}</script>\n</body></html>"
    )


def _int_param(query_string: str, name: str, default: int, maximum: int) -> int:
    values = parse_qs(query_string).get(name)
    raw = str(values[-1]).strip() if values else ""
    if not raw:
        return default
    value = int(raw)
    if value < 1 or value > maximum:
        raise ValueError(f"Expected {name} in range [1, {maximum}], got {value}")
    return value


def _unreadable(handler: Any) -> None:
    handler._write_json({"ok": False, "error": "decision ledger unreadable"}, status=HTTPStatus.SERVICE_UNAVAILABLE)


def write_decisions_page(handler: Any, version: str) -> None:
    handler._write_html(render_page(version))


def write_metrics_response(handler: Any, query_string: str) -> None:
    days = _int_param(query_string, "days", REVIEW_DAYS, MAX_DAYS)
    try:
        payload = metrics_payload(read_ledger(), days=days)
    except LedgerReadError:
        _unreadable(handler)
        return
    handler._write_json(payload)


def write_review_queue_response(handler: Any, query_string: str) -> None:
    days = _int_param(query_string, "days", REVIEW_DAYS, MAX_DAYS)
    size = _int_param(query_string, "size", REVIEW_SIZE, MAX_SIZE)
    try:
        items = select_review_queue(read_ledger(), days=days, size=size)
    except LedgerReadError:
        _unreadable(handler)
        return
    handler._write_json({"ok": True, "days": days, "items": items})


def write_review_label_response(handler: Any, payload: dict[str, Any]) -> None:
    ledger = DecisionLedger(default_ledger_path())
    try:
        result = record_review_label(
            ledger, decision_id=payload.get("decision_id"), item_ref=payload.get("item_ref"),
            question_id=payload.get("question_id"), verdict=payload.get("verdict"),
        )
    except LedgerReadError:
        _unreadable(handler)
        return
    handler._write_json(result)


__all__ = [
    "render_page",
    "write_decisions_page",
    "write_metrics_response",
    "write_review_label_response",
    "write_review_queue_response",
]

"""Read-only, scope-authorized decision summary; unknown is never healthy."""

from pathlib import Path
import os
import sqlite3
from urllib.parse import parse_qs

from memorymaster.core.capture_control import capture_state_path
from memorymaster.operations.review_attempt import read_review_state
from memorymaster.stores._storage_shared import connect_ro


def _dream_summary(path, scopes):
    if not path.is_file():
        return {"status": "empty", "last_processed": None, "resumable": 0, "historical": 0, "deferred": 0}
    where = " WHERE scope IN (" + ",".join("?" for _ in scopes) + ")" if scopes is not None else ""
    with connect_ro(path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(dream_captures)")}
        if not columns:
            return {"status": "unavailable"}
        eligibility = "resume_eligible" if "resume_eligible" in columns else "0"
        deferred = "deferred_reason='consolidate_budget'" if "deferred_reason" in columns else "0"
        row = conn.execute(f"""SELECT MAX(CASE WHEN state='applied' THEN updated_at END),
            COALESCE(SUM(state='extracted' AND {eligibility}=1),0),
            COALESCE(SUM(state='extracted' AND {eligibility}=0),0),
            COALESCE(SUM(state='extracted' AND {eligibility}=1 AND {deferred}),0), COUNT(*)
            FROM dream_captures{where}""", tuple(scopes or ())).fetchone()
    return dict(zip(("last_processed", "resumable", "historical", "deferred", "captures"), row))


def _profile_summary(service, scopes):
    if scopes is not None:
        return {"status": "unavailable", "reason": "Global profile progress is not scope-attributable"}
    with service.store.connect() as conn:
        row = conn.execute("""SELECT status, current_watermark, target_watermark
            FROM compiled_profile_runs ORDER BY id DESC LIMIT 1""").fetchone()
    return dict(row) if row else {"status": "empty"}


def decision_summary(service, *, scopes=None, ledger=None, review_root=None):
    authorized = service._effective_scope_allowlist(scopes)
    if hasattr(service.store, "dsn"):
        return {"ok": True, "status": "unavailable", "reason": "SQLite diagnostics only"}
    parts = {}
    for key, reader in (
        ("capture", lambda: _dream_summary(Path(ledger or capture_state_path()), authorized)),
        ("profile", lambda: _profile_summary(service, authorized)),
    ):
        try:
            parts[key] = reader()
        except (sqlite3.Error, OSError):
            parts[key] = {"status": "error", "reason": "Could not read diagnostic evidence"}
    review = ({"verdict": "UNAVAILABLE", "reason": "Global review is not scope-attributable"}
              if authorized is not None else read_review_state(Path(review_root or review_results_path())))
    # Never return attempt payloads: they can contain workspace paths or global counts.
    return {"ok": True, "scopes": authorized, **parts,
            "review": {key: review.get(key) for key in ("verdict", "reason", "fresh")}}


def write_summary_response(handler, query_string):
    query = parse_qs(query_string)
    scopes = [s.strip() for s in query.get("scope_allowlist", [""])[-1].split(",") if s.strip()]
    handler._write_json(decision_summary(handler._server.service, scopes=scopes or None))


def review_results_path():
    configured = os.environ.get("MEMORYMASTER_REVIEW_RESULTS", "")
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share"))
    return Path(configured) if configured else base / "MemoryMaster" / "operational-review" / "results"


SUMMARY_HTML = '''<section class="wide" id="decision-summary-section">
<div class="section-head"><div><h2>Decision summary</h2><div class="desc">Processing, review and profile evidence</div></div></div>
<div id="decision-summary" role="status" aria-live="polite">Loading decision summary...</div>
</section>'''

SUMMARY_JS = '''
function fillDecisionSummary(d){
 const c=d.capture||{},p=d.profile||{},r=d.review||{};
 const reasons={attempt_running:'Review in progress',attempt_missing_or_invalid:'No current review evidence',deadline_expired:'Review did not finish before its deadline',interval_expired:'Review evidence has expired',attempt_finished:'Latest review completed'};
 const stat=(label,value)=>'<div class="stat-item"><span class="stat-value">'+esc(value)+'</span><span class="stat-label">'+esc(label)+'</span></div>';
 const profile=p.status==='empty'?'Not started':p.status==='unavailable'?'Unavailable for this scope':p.status==='error'?'Read error':(p.status||'Unknown')+' '+(p.current_watermark||0)+' / '+(p.target_watermark||0);
 document.getElementById('decision-summary').innerHTML='<div class="stat-row">'+stat('Last capture processed',c.status==='error'?'Read error':c.last_processed||'No processed capture')+stat('Resumable',c.resumable??'Unknown')+stat('Deferred by budget',c.deferred??'Unknown')+stat('Historical retained',c.historical??'Unknown')+stat('Operational review',r.verdict||'Unavailable')+stat('Profile progress',profile)+'</div><div class="muted">'+esc(reasons[r.reason]||r.reason||d.reason||'')+'</div>';
}
async function refreshDecisionSummary(){
 const scopes=document.getElementById('retrieval-scope').value||'';
 try{fillDecisionSummary(await jget('/api/decision-summary?scope_allowlist='+encodeURIComponent(scopes)));}
 catch(e){showPanelFailure('decision-summary','decision summary',e);}
}
document.getElementById('retrieval-scope').addEventListener('change',refreshDecisionSummary);
refreshDecisionSummary();
'''

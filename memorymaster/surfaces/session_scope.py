"""CLI and dashboard surfaces for privacy-preserving session scope bindings."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from memorymaster.core.session_scope import SessionScopeRepository, validate_scope

SESSION_SCOPE_SECTION_HTML = """
<section>
<div class="section-head"><div class="icon icon-cyan">&#128279;</div><div><h2>Session Scopes</h2><div class="desc">Active privacy-preserving agent-to-scope bindings</div></div></div>
<div class="scroll"><table><thead><tr><th>Session hash</th><th>Agent</th><th>Platform</th><th>Scope</th><th>Source</th><th>Expires</th></tr></thead><tbody id="scope-bindings-body"><tr><td colspan="6" class="empty">No active bindings</td></tr></tbody></table></div>
</section>
"""

SESSION_SCOPE_FUNCTIONS_JS = """
function fillScopeBindings(d){const rows=Array.isArray(d.items)?d.items:[];const b=document.getElementById('scope-bindings-body');b.innerHTML=rows.map(r=>'<tr><td class="mono">'+esc(String(r.session_hash||'').slice(0,12))+'&#8230;</td><td class="mono">'+esc(r.source_agent||'-')+'</td><td>'+esc(r.platform||'-')+'</td><td class="mono">'+esc(r.scope||'user')+'</td><td>'+esc(r.binding_source||'-')+'</td><td class="mono" style="font-size:.75rem">'+esc(r.expires_at||'-')+'</td></tr>').join('')||'<tr><td colspan="6" class="empty">No active bindings</td></tr>';}
"""


def hydrate_dashboard_html(html: str) -> str:
    """Insert session-scope assets without growing the dashboard facade."""
    return html.replace("__SESSION_SCOPE_SECTION__", SESSION_SCOPE_SECTION_HTML).replace(
        "__SESSION_SCOPE_FUNCTIONS__", SESSION_SCOPE_FUNCTIONS_JS
    )


def session_scope_payload(service: Any, *, limit: int = 100) -> dict[str, Any]:
    """Return bounded active binding metadata without raw session identifiers."""
    repository = SessionScopeRepository(service.store.db_path)
    items = [item.to_dict() for item in repository.list_active(limit=limit)]
    return {"ok": True, "rows": len(items), "items": items}


def write_session_scope_response(handler: Any, query_string: str) -> None:
    """Write active scope bindings through the authenticated local dashboard."""
    query = parse_qs(query_string)
    raw_limit = str((query.get("limit") or ["100"])[-1]).strip()
    limit = int(raw_limit or "100")
    if limit < 1 or limit > 100:
        raise ValueError("Expected integer in range [1, 100]")
    handler._write_json(session_scope_payload(handler._server.service, limit=limit))


def _show(repository: SessionScopeRepository, session_id: str | None) -> dict[str, Any]:
    if session_id:
        items = repository.history(session_id)
    else:
        items = repository.list_active()
    return {"ok": True, "rows": len(items), "items": [item.to_dict() for item in items]}


def _bind(repository: SessionScopeRepository, args: argparse.Namespace) -> dict[str, Any]:
    scope = validate_scope(args.scope, allow_global=bool(args.allow_global))
    workspace = Path(args.workspace).resolve()
    binding = repository.bind(
        args.session_id,
        scope=scope,
        source_agent=args.source_agent,
        platform=args.platform,
        binding_source="explicit",
        workspace_slug=workspace.name if workspace.is_dir() else None,
        task_label=args.task_label,
        ttl_seconds=args.ttl_seconds,
        replace=True,
    )
    return {"ok": True, **binding.to_dict()}


def _clear(repository: SessionScopeRepository, args: argparse.Namespace) -> dict[str, Any]:
    ended = repository.end(
        args.session_id,
        source_agent=args.source_agent or None,
        platform=args.platform or None,
    )
    return {"ok": True, "ended": ended}


def handle_session_scope(
    args: argparse.Namespace, service: Any, parser: argparse.ArgumentParser, effective_db: str
) -> int:
    """Dispatch session-scope show, bind, and clear operations."""
    service.init_db()
    repository = SessionScopeRepository(effective_db)
    if args.session_scope_action == "show":
        payload = _show(repository, args.session_id)
    elif args.session_scope_action == "bind":
        payload = _bind(repository, args)
    else:
        payload = _clear(repository, args)
    if args.json_output:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    elif args.session_scope_action == "show":
        print(f"session scope bindings: {payload['rows']}")
    elif args.session_scope_action == "clear":
        print(f"session scope bindings ended: {payload['ended']}")
    else:
        print(f"session scope bound: {payload['scope']} ({payload['binding_source']})")
    return 0

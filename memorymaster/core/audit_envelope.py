"""Canonical attributable audit metadata for operational actions."""

from __future__ import annotations

from datetime import datetime, timezone


def build_audit_envelope(
    *,
    principal: str,
    tenant_id: str | None,
    role: str,
    request_id: str,
    session_id: str,
    action: str,
    target: str,
    result: str,
) -> dict[str, str | None]:
    required = {
        "principal": principal,
        "role": role,
        "request_id": request_id,
        "session_id": session_id,
        "action": action,
        "target": target,
        "result": result,
    }
    blanks = [name for name, value in required.items() if not str(value or "").strip()]
    if blanks:
        raise ValueError(f"audit envelope requires: {', '.join(blanks)}")
    return {
        **{name: str(value).strip() for name, value in required.items()},
        "tenant_id": str(tenant_id).strip() if tenant_id else None,
        "occurred_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }

"""HTTP-layer authentication, CSRF, and bind-safety for the dashboard (v3.19.0-H2).

The dashboard's prior posture was "trusted local-only" — zero HTTP auth,
default loopback bind. Anyone who could reach the port had full read +
operator-control. This module adds opt-in token auth with viewer/operator
role separation, CSRF for browser POSTs, and refusal to bind non-loopback
hosts without an explicit secret.

Backwards compatibility: when no auth secrets are configured (both
``MEMORYMASTER_DASHBOARD_TOKEN_VIEWER`` and ``MEMORYMASTER_DASHBOARD_TOKEN_OPERATOR``
are empty), the dashboard runs in legacy mode — no auth enforced. Bind
safety still applies: legacy mode refuses non-loopback bind unless the
operator explicitly opts in with ``MEMORYMASTER_DASHBOARD_UNSAFE_BIND=1``.

Env vars:
    MEMORYMASTER_DASHBOARD_TOKEN_VIEWER     — bearer token granting read-only access
    MEMORYMASTER_DASHBOARD_TOKEN_OPERATOR   — bearer token granting full mutating access
    MEMORYMASTER_DASHBOARD_UNSAFE_BIND      — set to 1 to allow non-loopback bind
                                              without an auth secret (logs WARNING)
"""
from __future__ import annotations

import hmac
import logging
import os
from dataclasses import dataclass
from enum import Enum
from memorymaster.surfaces.dashboard_origins import (
    WILDCARD_HOSTS, allowed_origins, explicit_origins, host_allowed, is_loopback,
    normalize_origin, single_header,
)

logger = logging.getLogger(__name__)


class DashboardRole(str, Enum):
    VIEWER = "viewer"
    OPERATOR = "operator"


# Routes that require operator role regardless of HTTP method. POST routes
# always require operator role separately. This list captures GET routes that
# are mutating-adjacent (start/stop operator control, stream that may carry
# sensitive operator events).
OPERATOR_ONLY_GET_ROUTES: frozenset[str] = frozenset({
    "/api/operator/control",  # POST in practice but pin it here for completeness
    "/api/operator/stream",   # SSE — operator-only since it streams operator events
})

# POST routes — all require operator.
POST_OPERATOR_ROUTES: frozenset[str] = frozenset({
    "/api/triage/action",
    "/api/operator/control",
    "/api/action-proposals/status",
})


@dataclass(frozen=True)
class AuthDecision:
    """Outcome of an authentication or authorization check."""

    ok: bool
    role: DashboardRole | None = None
    reason: str = ""
    status: int = 200


class BindUnsafeError(RuntimeError):
    """Raised when the dashboard refuses to bind a non-loopback host without auth."""


def _env_token(name: str) -> str:
    return os.environ.get(name, "").strip()


def legacy_mode() -> bool:
    """True when no auth secrets are configured (back-compat path).

    Legacy mode preserves the pre-v3.19 behaviour for loopback bind — no
    auth check. Origin and Host validation still apply regardless.
    """
    return not (
        _env_token("MEMORYMASTER_DASHBOARD_TOKEN_VIEWER")
        or _env_token("MEMORYMASTER_DASHBOARD_TOKEN_OPERATOR")
    )


def _extract_bearer(headers) -> str:
    raw = headers.get("Authorization", "") if headers else ""
    if not raw or not raw.lower().startswith("bearer "):
        return ""
    return raw[7:].strip()


def authenticate(headers) -> AuthDecision:
    """Map an incoming request's Authorization header to a role.

    Behaviour:
    - Legacy mode (no tokens set): return ok with role=operator (no enforcement).
    - Missing token: 401 (``missing_token``).
    - Token matches operator: ok with role=operator.
    - Token matches viewer: ok with role=viewer.
    - Token unrecognized: 401 (``invalid_token``).

    Constant-time comparison (``hmac.compare_digest``) prevents timing attacks.
    """
    if legacy_mode():
        return AuthDecision(True, role=DashboardRole.OPERATOR)

    token = _extract_bearer(headers)
    if not token:
        return AuthDecision(False, reason="missing_token", status=401)

    op_token = _env_token("MEMORYMASTER_DASHBOARD_TOKEN_OPERATOR")
    vw_token = _env_token("MEMORYMASTER_DASHBOARD_TOKEN_VIEWER")

    if op_token and hmac.compare_digest(token, op_token):
        return AuthDecision(True, role=DashboardRole.OPERATOR)
    if vw_token and hmac.compare_digest(token, vw_token):
        return AuthDecision(True, role=DashboardRole.VIEWER)
    return AuthDecision(False, reason="invalid_token", status=401)


def authorize(decision: AuthDecision, *, method: str, route: str) -> AuthDecision:
    """Refine an authentication decision against the role-vs-route policy.

    Returns 403 (``role_required_operator``) for routes where viewer is
    insufficient. Passes ``decision`` through unchanged on success.
    """
    if not decision.ok:
        return decision

    method_u = method.upper()
    operator_required = (
        method_u == "POST"
        or route in OPERATOR_ONLY_GET_ROUTES
        or route in POST_OPERATOR_ROUTES
    )
    if operator_required and decision.role != DashboardRole.OPERATOR:
        return AuthDecision(
            False,
            role=decision.role,
            reason="role_required_operator",
            status=403,
        )
    return decision


def check_csrf(headers, *, configured_host_port: str | None, origins=None) -> AuthDecision:
    """Compare normalized origins exactly, including in token-free local mode."""
    try:
        origin = single_header(headers, "Origin")
        referer = single_header(headers, "Referer") if origin is None else None
        if origin is None and referer is None:
            return AuthDecision(True)  # compatible local non-browser clients
        if origins is None:
            _, host, port = normalize_origin(f"http://{configured_host_port}")
            origins = allowed_origins(host, port)
        if normalize_origin(origin if origin is not None else referer, referer=origin is None) in origins:
            return AuthDecision(True)
    except (ValueError, UnicodeError, TypeError):
        pass
    return AuthDecision(False, reason="csrf_origin_mismatch", status=403)


def check_host(headers, *, origins) -> AuthDecision:
    try:
        value = single_header(headers, "Host")
        if value and host_allowed(value, origins):
            return AuthDecision(True)
    except (ValueError, UnicodeError):
        pass
    return AuthDecision(False, reason="host_mismatch", status=403)


def check_bind_safety(host: str) -> None:
    """Refuse non-loopback bind without an auth secret or explicit opt-in.

    Raises ``BindUnsafeError`` if all of: (a) host is non-loopback,
    (b) no auth tokens configured, (c) ``MEMORYMASTER_DASHBOARD_UNSAFE_BIND``
    not set. Otherwise returns silently. Logs a WARNING for the unsafe opt-in
    case so operators see they're running exposed.
    """
    try:
        explicit = explicit_origins()
    except (ValueError, UnicodeError) as exc:
        raise BindUnsafeError("Invalid MEMORYMASTER_DASHBOARD_ALLOWED_ORIGINS") from exc
    if host in WILDCARD_HOSTS and not explicit:
        raise BindUnsafeError("Wildcard bind requires MEMORYMASTER_DASHBOARD_ALLOWED_ORIGINS")
    if is_loopback(host):
        return
    if not legacy_mode():
        return  # token-based auth is enforced; non-loopback bind is acceptable

    unsafe_raw = os.environ.get("MEMORYMASTER_DASHBOARD_UNSAFE_BIND", "").strip().lower()
    if unsafe_raw in {"1", "true", "yes", "on"}:
        logger.warning(
            "dashboard binding to non-loopback host '%s' with no auth secret "
            "and MEMORYMASTER_DASHBOARD_UNSAFE_BIND=1 — running exposed",
            host,
        )
        return

    raise BindUnsafeError(
        f"Refusing to bind dashboard to non-loopback host '{host}' without auth secret. "
        f"Set MEMORYMASTER_DASHBOARD_TOKEN_OPERATOR (and optionally "
        f"MEMORYMASTER_DASHBOARD_TOKEN_VIEWER), or explicitly opt-in with "
        f"MEMORYMASTER_DASHBOARD_UNSAFE_BIND=1."
    )

from __future__ import annotations

import ipaddress
import os
from pathlib import Path
import re
from datetime import date
from urllib.parse import urlparse

from memorymaster.models import Claim
from memorymaster.lifecycle import transition_claim

_EMAIL_RE = re.compile(r"^[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}$", re.IGNORECASE)
_HOST_RE = re.compile(r"^(?=.{1,253}$)(?!-)[A-Z0-9-]{1,63}(?<!-)(?:\.(?!-)[A-Z0-9-]{1,63}(?<!-))*$", re.IGNORECASE)
_SEMVER_RE = re.compile(r"^v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.\-]+)?$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_PHONE_RE = re.compile(r"^\+?[1-9]\d{6,14}$")
_ISO2_RE = re.compile(r"^[A-Z]{2}$")
_WORKSPACE_SKIP_DIRS = {
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    ".tmp_cases",
    ".tmp_pytest",
    ".pytest_cache",
    "artifacts",
}
_WORKSPACE_SKIP_SUFFIXES = {
    ".db",
    ".sqlite",
    ".sqlite3",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".pdf",
    ".zip",
    ".gz",
    ".tar",
    ".7z",
    ".woff",
    ".woff2",
    ".ttf",
    ".exe",
    ".dll",
    ".bin",
    ".mp4",
    ".mp3",
}


def _path_exists(path_value: str) -> bool:
    try:
        return Path(path_value).exists()
    except Exception:
        return False


def _is_valid_ipv4(value: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(value), ipaddress.IPv4Address)
    except ValueError:
        return False


def _is_valid_ipv6(value: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(value), ipaddress.IPv6Address)
    except ValueError:
        return False


def _is_valid_cidr(value: str) -> bool:
    try:
        ipaddress.ip_network(value.strip(), strict=False)
        return True
    except ValueError:
        return False


def _is_valid_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _is_valid_email(value: str) -> bool:
    return bool(_EMAIL_RE.match(value.strip()))


def _is_valid_hostname(value: str) -> bool:
    candidate = value.strip().rstrip(".")
    if not candidate or "." not in candidate:
        return False
    return bool(_HOST_RE.match(candidate))


def _is_valid_port(value: str) -> bool:
    try:
        port = int(value.strip())
    except (TypeError, ValueError):
        return False
    return 1 <= port <= 65535


def _is_valid_iso_date(value: str) -> bool:
    try:
        parsed = date.fromisoformat(value.strip())
    except (TypeError, ValueError):
        return False
    return parsed.year >= 1970


def _is_semver(value: str) -> bool:
    return bool(_SEMVER_RE.match(value.strip()))


def _is_sha256(value: str) -> bool:
    return bool(_SHA256_RE.match(value.strip()))


def _is_uuid(value: str) -> bool:
    return bool(_UUID_RE.match(value.strip()))


def _is_phone_e164ish(value: str) -> bool:
    candidate = value.strip().replace(" ", "").replace("-", "")
    return bool(_PHONE_RE.match(candidate))


def _is_iso2(value: str) -> bool:
    return bool(_ISO2_RE.match(value.strip().upper()))


def _workspace_contains(workspace_root: Path, needle: str, max_files: int = 300, max_size: int = 1_000_000) -> bool:
    checked = 0
    lower_needle = needle.lower()
    for root, dirs, files in os.walk(workspace_root):
        dirs[:] = [d for d in dirs if d not in _WORKSPACE_SKIP_DIRS]
        for name in files:
            suffix = Path(name).suffix.lower()
            if suffix in _WORKSPACE_SKIP_SUFFIXES:
                continue
            checked += 1
            if checked > max_files:
                return False
            p = Path(root) / name
            try:
                if p.stat().st_size > max_size:
                    continue
                with p.open("r", encoding="utf-8", errors="ignore") as handle:
                    if lower_needle in handle.read().lower():
                        return True
            except OSError:
                continue
    return False


def _merge_claims(primary: list[Claim], secondary: list[Claim]) -> list[Claim]:
    seen: set[int] = set()
    merged: list[Claim] = []
    for claim in primary + secondary:
        if claim.id in seen:
            continue
        seen.add(claim.id)
        merged.append(claim)
    return merged


def run(
    store,
    workspace_root: Path,
    limit: int = 200,
    revalidation_claims: list[Claim] | None = None,
    policy_mode: str = "legacy",
) -> dict[str, int]:
    if policy_mode == "legacy":
        candidates = store.list_claims(
            status_in=["candidate", "confirmed"],
            limit=limit,
            include_archived=False,
            include_citations=False,
        )
    else:
        candidate_claims = store.list_claims(
            status_in=["candidate"],
            limit=limit,
            include_archived=False,
            include_citations=False,
        )
        due_claims = [
            claim
            for claim in (revalidation_claims or [])
            if claim.status in {"confirmed", "stale", "conflicted"}
        ]
        candidates = _merge_claims(candidate_claims, due_claims)

    checked = 0
    boosts = 0
    drops = 0
    hard_conflicts = 0
    revalidation_checked = 0
    predicate_checks: dict[str, int] = {
        "ipv4_checked": 0,
        "ipv6_checked": 0,
        "cidr_checked": 0,
        "url_checked": 0,
        "email_checked": 0,
        "host_checked": 0,
        "port_checked": 0,
        "date_checked": 0,
        "semver_checked": 0,
        "sha256_checked": 0,
        "uuid_checked": 0,
        "phone_checked": 0,
        "country_code_checked": 0,
    }

    for claim in candidates:
        checked += 1
        if claim.status in {"confirmed", "stale", "conflicted"}:
            revalidation_checked += 1
        confidence = claim.confidence
        payload: dict[str, object] = {}
        hard_fail = False
        object_value = claim.object_value or ""

        if claim.predicate == "path" and object_value:
            exists = _path_exists(object_value)
            payload["path_exists"] = exists
            confidence += 0.12 if exists else -0.10

        if claim.predicate == "ip_address" and object_value:
            predicate_checks["ipv4_checked"] += 1
            valid = _is_valid_ipv4(object_value)
            payload["ip_valid"] = valid
            confidence += 0.10 if valid else -0.35
            hard_fail = hard_fail or (not valid)

        if claim.predicate in {"ipv6", "ip_v6_address"} and object_value:
            predicate_checks["ipv6_checked"] += 1
            valid = _is_valid_ipv6(object_value)
            payload["ipv6_valid"] = valid
            confidence += 0.10 if valid else -0.35
            hard_fail = hard_fail or (not valid)

        if claim.predicate in {"cidr", "network_cidr"} and object_value:
            predicate_checks["cidr_checked"] += 1
            valid = _is_valid_cidr(object_value)
            payload["cidr_valid"] = valid
            confidence += 0.08 if valid else -0.30
            hard_fail = hard_fail or (not valid)

        if claim.predicate == "url" and object_value:
            predicate_checks["url_checked"] += 1
            valid = _is_valid_url(object_value)
            payload["url_valid"] = valid
            confidence += 0.10 if valid else -0.35
            hard_fail = hard_fail or (not valid)

        if claim.predicate in {"email", "support_email", "contact_email"} and object_value:
            predicate_checks["email_checked"] += 1
            valid = _is_valid_email(object_value)
            payload["email_valid"] = valid
            confidence += 0.08 if valid else -0.25
            hard_fail = hard_fail or (not valid)

        if claim.predicate in {"hostname", "host"} and object_value:
            predicate_checks["host_checked"] += 1
            valid = _is_valid_hostname(object_value)
            payload["host_valid"] = valid
            confidence += 0.08 if valid else -0.20
            hard_fail = hard_fail or (not valid)

        if claim.predicate == "port" and object_value:
            predicate_checks["port_checked"] += 1
            valid = _is_valid_port(object_value)
            payload["port_valid"] = valid
            confidence += 0.06 if valid else -0.25
            hard_fail = hard_fail or (not valid)

        if claim.predicate in {"deadline", "date"} and object_value:
            predicate_checks["date_checked"] += 1
            valid = _is_valid_iso_date(object_value)
            payload["date_valid"] = valid
            confidence += 0.05 if valid else -0.18

        if claim.predicate in {"version", "semver"} and object_value:
            predicate_checks["semver_checked"] += 1
            valid = _is_semver(object_value)
            payload["semver_valid"] = valid
            confidence += 0.05 if valid else -0.10

        if claim.predicate in {"sha256", "file_hash"} and object_value:
            predicate_checks["sha256_checked"] += 1
            valid = _is_sha256(object_value)
            payload["sha256_valid"] = valid
            confidence += 0.08 if valid else -0.18
            hard_fail = hard_fail or (not valid)

        if claim.predicate in {"uuid", "uuid_v4", "object_id"} and object_value:
            predicate_checks["uuid_checked"] += 1
            valid = _is_uuid(object_value)
            payload["uuid_valid"] = valid
            confidence += 0.06 if valid else -0.22
            hard_fail = hard_fail or (not valid)

        if claim.predicate in {"phone", "phone_number"} and object_value:
            predicate_checks["phone_checked"] += 1
            valid = _is_phone_e164ish(object_value)
            payload["phone_valid"] = valid
            confidence += 0.05 if valid else -0.20
            hard_fail = hard_fail or (not valid)

        if claim.predicate in {"country_code", "iso_country_code"} and object_value:
            predicate_checks["country_code_checked"] += 1
            valid = _is_iso2(object_value)
            payload["country_code_valid"] = valid
            confidence += 0.04 if valid else -0.10

        # Keep workspace lexical probes limited to technical predicates.
        # For planning/contact style predicates (email/deadline/address, etc.),
        # this heuristic can bias old values that happen to appear in files.
        if object_value and len(object_value) >= 4 and claim.predicate in {
            "path",
            "ip_address",
            "ipv6",
            "ip_v6_address",
            "cidr",
            "network_cidr",
            "url",
            "location_hint",
            "hostname",
            "host",
            "file_hash",
            "sha256",
        }:
            found_in_workspace = _workspace_contains(workspace_root, object_value)
            payload["workspace_match"] = found_in_workspace
            confidence += 0.10 if found_in_workspace else -0.02

        next_conf = max(0.0, min(1.0, confidence))
        if next_conf > claim.confidence:
            boosts += 1
        elif next_conf < claim.confidence:
            drops += 1
        store.set_confidence(claim.id, next_conf, details=f"deterministic_adjust={next_conf - claim.confidence:+.3f}")
        store.record_event(
            claim_id=claim.id,
            event_type="deterministic_validator",
            from_status=claim.status,
            to_status=claim.status,
            details="deterministic_checks_completed",
            payload=payload,
        )

        if hard_fail:
            transition_claim(
                store,
                claim_id=claim.id,
                to_status="conflicted",
                reason="deterministic hard fail (invalid format)",
                event_type="deterministic_validator",
            )
            hard_conflicts += 1

    return {
        "checked": checked,
        "boosted": boosts,
        "dropped": drops,
        "hard_conflicted": hard_conflicts,
        "revalidation_checked": revalidation_checked,
        "predicate_checks": predicate_checks,
    }

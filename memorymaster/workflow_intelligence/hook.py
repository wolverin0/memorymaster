"""Fail-soft, content-free completion receipt hook (off by default)."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

from .adapters import action_kind
from .analysis import VERIFICATION_TIERS
from .storage import WorkflowStore, utc_now


_COMPLETION = re.compile(r"(?i)\b(done|complete(?:d)?|fixed|working|resolved)\b")
_DEPLOYED = re.compile(r"(?i)\b(deployed|in production|live)\b")
_UI = re.compile(r"(?i)\b(ui|ux|frontend|visual|screen|page)\b")
_NATURAL = re.compile(r"(?i)\b(natural run|scheduled run|scheduler worked|externally accepted)\b")
_A2A = re.compile(r"(?i)\b(a2a|receipt|wezbridge)\b")
_MODES = {"off", "shadow", "advisory"}
_ACTION_KINDS = {"mutation", "read", "verification", "command", "tool", "unknown"}
_PROVIDERS = {"claude", "codex", "wezbridge", "unknown"}


def _tier(actions: list[dict[str, Any]]) -> str:
    observed = {
        str(item.get("verification_tier") or "")
        for item in actions
        if item.get("status") == "success" and item.get("kind") == "verification"
    }
    for name in reversed(VERIFICATION_TIERS):
        if name in observed:
            return name
    if any(item.get("kind") == "verification" and item.get("status") == "success" for item in actions):
        return "unit"
    return "none"


def evaluate_payload(payload: dict[str, Any]) -> dict[str, Any]:
    mode = os.environ.get("MEMORYMASTER_WORKFLOW_RECEIPTS", "off").strip().lower()
    mode = mode if mode in _MODES else "off"
    if mode == "off":
        return {"mode": "off", "warnings": []}
    payload = _hydrate_from_transcript(payload)
    actions = payload.get("actions") or payload.get("tool_calls") or []
    actions = [item for item in actions if isinstance(item, dict)]
    mutation = any(item.get("kind") == "mutation" and item.get("status", "success") != "failed" for item in actions)
    text = str(payload.get("assistant_text") or payload.get("last_assistant_message") or "")
    tier = _tier(actions)
    warnings = _warnings(text, actions, mutation, tier)
    _write_receipt(payload, mode, actions, mutation, tier, warnings, bool(_COMPLETION.search(text)))
    return {"mode": mode, "warnings": warnings, "emit_advisory": mode == "advisory" and bool(warnings)}


def _hydrate_from_transcript(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("actions") or payload.get("tool_calls"):
        return payload
    path = Path(str(payload.get("transcript_path") or ""))
    if not _allowed_transcript(path):
        return payload
    rows = _tail_json(path)
    provider = str(payload.get("provider") or "claude").lower()
    actions, assistant_text = (
        _codex_delta(rows) if provider == "codex" else _claude_delta(rows)
    )
    hydrated = dict(payload)
    hydrated["actions"] = actions
    hydrated["assistant_text"] = assistant_text
    return hydrated


def _allowed_transcript(path: Path) -> bool:
    if path.suffix.lower() != ".jsonl" or not path.is_file():
        return False
    configured = [
        Path(item).expanduser() for item in os.environ.get(
            "MEMORYMASTER_WORKFLOW_TRANSCRIPT_ROOTS", ""
        ).split(os.pathsep) if item.strip()
    ]
    roots = configured or [Path.home() / ".claude", Path.home() / ".codex"]
    try:
        resolved = path.resolve()
        return any(resolved.is_relative_to(root.resolve()) for root in roots)
    except OSError:
        return False


def _tail_json(path: Path, limit: int = 256_000) -> list[dict[str, Any]]:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - limit))
            raw = handle.read(limit)
    except OSError:
        return []
    if size > limit and b"\n" in raw:
        raw = raw.split(b"\n", 1)[1]
    rows: list[dict[str, Any]] = []
    for line in raw.splitlines():
        try:
            item = json.loads(line.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(item.get("text") or "")
        for item in content
        if isinstance(item, dict)
        and item.get("type") in {"text", "input_text", "output_text"}
    )


def _last_user_boundary(rows: list[dict[str, Any]], provider: str) -> int:
    boundary = 0
    for index, entry in enumerate(rows):
        payload = entry.get("payload") if provider == "codex" else entry.get("message")
        if not isinstance(payload, dict):
            continue
        if payload.get("role") == "user" and _content_text(payload.get("content")):
            boundary = index
    return boundary


def _normalized_action(name: str, arguments: object, call_id: str) -> dict[str, Any]:
    kind, family = action_kind(name, arguments)
    return {
        "kind": kind,
        "name": name,
        "command_family": family,
        "status": "unknown",
        "call_id": call_id,
        "verification_tier": _verification_tier(f"{name} {family} {arguments}"),
    }


def _verification_tier(text: str) -> str:
    checks = (
        ("natural_external_acceptance", r"(?i)(natural[-_ ]run|external acceptance|recipient ack)"),
        ("deployed_identity", r"(?i)(production|deployed).*(identity|sha|version)"),
        ("browser_visual", r"(?i)(playwright|cypress|browser|screenshot|visual)"),
        ("runtime_api", r"(?i)(curl|http|api|runtime|smoke)"),
        ("integration_build", r"(?i)(build|integration|e2e)"),
        ("unit", r"(?i)(pytest|unittest|jest|vitest|cargo test|go test)"),
        ("syntax_static", r"(?i)(ruff|mypy|typecheck|tsc|compile|syntax)"),
    )
    return next((tier for tier, pattern in checks if re.search(pattern, text)), "none")


def _claude_delta(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    actions: list[dict[str, Any]] = []
    pending: dict[str, int] = {}
    assistant: list[str] = []
    for entry in rows[_last_user_boundary(rows, "claude"):]:
        message = entry.get("message") if isinstance(entry.get("message"), dict) else entry
        content = message.get("content")
        if message.get("role") == "assistant":
            assistant.append(_content_text(content))
        for item in content if isinstance(content, list) else []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "tool_use":
                call_id = str(item.get("id") or len(actions))
                pending[call_id] = len(actions)
                actions.append(_normalized_action(
                    str(item.get("name") or "tool"), item.get("input"), call_id
                ))
            elif item.get("type") == "tool_result" and str(item.get("tool_use_id")) in pending:
                action = actions[pending[str(item.get("tool_use_id"))]]
                action["status"] = "failed" if item.get("is_error") else "success"
    return actions, "\n".join(filter(None, assistant))[-4_000:]


def _codex_delta(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    actions: list[dict[str, Any]] = []
    pending: dict[str, int] = {}
    assistant: list[str] = []
    for entry in rows[_last_user_boundary(rows, "codex"):]:
        payload = entry.get("payload")
        if entry.get("type") != "response_item" or not isinstance(payload, dict):
            continue
        kind = str(payload.get("type") or "")
        if kind == "message" and payload.get("role") == "assistant":
            assistant.append(_content_text(payload.get("content")))
        elif kind in {"function_call", "custom_tool_call"}:
            call_id = str(payload.get("call_id") or payload.get("id") or len(actions))
            pending[call_id] = len(actions)
            actions.append(_normalized_action(
                str(payload.get("name") or payload.get("tool_name") or "tool"),
                payload.get("arguments") or payload.get("input"), call_id,
            ))
        elif kind in {"function_call_output", "custom_tool_call_output"}:
            call_id = str(payload.get("call_id") or payload.get("id") or "")
            if call_id in pending:
                output = str(payload.get("output") or "")
                actions[pending[call_id]]["status"] = "failed" if re.search(
                    r"(?i)(exit(?:_code)?[=: ]+[1-9]|error|failed)", output
                ) else "success"
    return actions, "\n".join(filter(None, assistant))[-4_000:]


def _warnings(
    text: str, actions: list[dict[str, Any]], mutation: bool, tier: str,
) -> list[str]:
    if not mutation:
        return []
    warnings: list[str] = []
    if _COMPLETION.search(text) and tier == "none":
        warnings.append("completion_without_verification")
    if _DEPLOYED.search(text) and VERIFICATION_TIERS.index(tier) < VERIFICATION_TIERS.index("deployed_identity"):
        warnings.append("deployment_without_runtime_identity")
    if _UI.search(text) and VERIFICATION_TIERS.index(tier) < VERIFICATION_TIERS.index("browser_visual"):
        warnings.append("ui_claim_without_browser_verification")
    if _NATURAL.search(text) and tier != "natural_external_acceptance":
        warnings.append("natural_run_claim_without_observation")
    statuses = {str(item.get("a2a_status") or "") for item in actions}
    if _A2A.search(text) and not ({"result", "ack"} <= statuses):
        warnings.append("a2a_receipt_without_result_ack")
    return warnings


def _write_receipt(
    payload: dict[str, Any], mode: str, actions: list[dict[str, Any]], mutation: bool,
    tier: str, warnings: list[str], completion: bool,
) -> None:
    session = str(payload.get("session_id") or payload.get("thread_id") or "unknown")
    session_hash = hashlib.sha256(session.encode("utf-8", errors="replace")).hexdigest()
    counts = Counter(
        kind if (kind := str(item.get("kind") or "unknown")) in _ACTION_KINDS else "unknown"
        for item in actions
    )
    provider = str(payload.get("provider") or "unknown").lower()
    provider = provider if provider in _PROVIDERS else "unknown"
    store = WorkflowStore()
    try:
        store.insert_receipt({
            "receipt_id": "receipt-" + uuid.uuid4().hex,
            "provider": provider,
            "session_hash": session_hash,
            "mode": mode,
            "observed_at": utc_now(),
            "mutation_seen": mutation,
            "verification_tier": tier,
            "completion_claimed": completion,
            "warning_codes": warnings,
            "action_counts": dict(sorted(counts.items())),
        })
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.parse_args(argv)
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            return 0
        result = evaluate_payload(payload)
        if result.get("emit_advisory"):
            print(json.dumps({"workflow_warnings": result["warnings"]}, sort_keys=True))
    except Exception:
        return 0
    return 0


__all__ = ["evaluate_payload", "main"]


if __name__ == "__main__":  # pragma: no cover - exercised by installed hook process
    raise SystemExit(main())

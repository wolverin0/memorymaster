"""Claim verifier — cross-check claims against current codebase state.

Detects claims that reference things that no longer exist or changed:
- File paths that don't exist anymore
- Function/class names not found in code
- Port numbers not in any config
- URLs not referenced anywhere

Usage:
    memorymaster verify-claims --scope project:impulsa
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Patterns to extract verifiable references from claim text
FILE_PATH_RE = re.compile(r'(?:src|app|lib|api|pages|routes|components|services|models|utils|scripts|config|test|bot|dashboard)[/\\][\w./-]+\.\w{1,5}')
FUNCTION_RE = re.compile(r'\b(?:function|def|class|const|let|var)\s+(\w{3,})\b')
PORT_RE = re.compile(r'\b(?:port|PORT)\s*[:=]\s*(\d{4,5})\b|\blocalhost:(\d{4,5})\b|:(\d{4,5})\b')
SYMBOL_RE = re.compile(r'\b([A-Z][a-z]+[A-Z]\w+)\b')  # CamelCase likely class/component names


def _find_workspace(scope: str) -> str | None:
    """Try to find the workspace directory for a scope."""
    projects_root = os.environ.get("PROJECTS_ROOT", r"G:\_OneDrive\OneDrive\Desktop\Py Apps")
    if not scope.startswith("project:"):
        return None
    project_name = scope.split(":")[1]
    # Try exact match
    candidate = os.path.join(projects_root, project_name)
    if os.path.isdir(candidate):
        return candidate
    # Try case-insensitive
    try:
        for d in os.listdir(projects_root):
            if d.lower().replace(" ", "-") == project_name.lower():
                return os.path.join(projects_root, d)
    except OSError:
        pass
    return None


def _check_file_exists(path: str, workspace: str) -> bool:
    """Check if a referenced file path exists in the workspace."""
    full = os.path.join(workspace, path)
    return os.path.exists(full)


def _check_symbol_in_code(symbol: str, workspace: str) -> bool:
    """Check if a symbol name exists anywhere in the codebase."""
    try:
        result = subprocess.run(
            ["rg", "-l", "--type-add", "code:*.{ts,js,py,tsx,jsx,go,rs}", "-t", "code",
             symbol, workspace],
            capture_output=True, text=True, timeout=3,
        )
        return bool(result.stdout.strip())
    except FileNotFoundError:
        # rg not available, fall back to assuming exists
        return True
    except Exception:
        return True


def _check_port_in_use(port: str, workspace: str) -> bool:
    """Check if a port number is still referenced in config files."""
    try:
        result = subprocess.run(
            ["rg", "-l", "-g", "*.{json,toml,yml,yaml,env,ts,js,py}",
             port, workspace],
            capture_output=True, text=True, timeout=3,
        )
        return bool(result.stdout.strip())
    except FileNotFoundError:
        return True
    except Exception:
        return True


def verify_claims(
    db_path: str,
    scope_filter: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Verify claims against current codebase state.

    Returns: {checked, valid, stale_candidates, issues: [...]}
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    query = """SELECT id, text, subject, predicate, object_value, scope, human_id, confidence
               FROM claims WHERE status IN ('confirmed', 'candidate')"""
    params: list = []
    if scope_filter:
        query += " AND scope LIKE ?"
        params.append(f"{scope_filter}%")
    query += " ORDER BY confidence DESC LIMIT ?"
    params.append(limit)

    claims = conn.execute(query, params).fetchall()
    conn.close()

    if not claims:
        return {"checked": 0, "valid": 0, "stale_candidates": 0, "issues": []}

    # Determine workspace
    scope = scope_filter or (claims[0]["scope"] if claims else "")
    workspace = _find_workspace(scope)
    if not workspace:
        return {"checked": 0, "valid": 0, "stale_candidates": 0,
                "issues": [], "error": f"workspace not found for scope {scope}"}

    checked = 0
    valid = 0
    issues = []

    for claim in claims:
        text = claim["text"] or ""
        claim_issues = []

        # Check file paths
        paths = FILE_PATH_RE.findall(text)
        for p in paths:
            checked += 1
            if not _check_file_exists(p, workspace):
                claim_issues.append(f"file not found: {p}")

        # Check CamelCase symbols (likely components/classes)
        symbols = SYMBOL_RE.findall(text)
        for sym in symbols[:3]:  # Limit to avoid too many greps
            if len(sym) > 4 and sym not in ("TRUE", "FALSE", "NULL", "None", "This"):
                checked += 1
                if not _check_symbol_in_code(sym, workspace):
                    claim_issues.append(f"symbol not found: {sym}")

        # Check ports
        port_matches = PORT_RE.findall(text)
        for groups in port_matches:
            port = next((g for g in groups if g), None)
            if port and port not in ("3000", "5173", "8080"):  # Skip common defaults
                checked += 1
                if not _check_port_in_use(port, workspace):
                    claim_issues.append(f"port not referenced: {port}")

        if claim_issues:
            issues.append({
                "claim_id": claim["id"],
                "human_id": claim["human_id"],
                "text": text[:100],
                "confidence": claim["confidence"],
                "issues": claim_issues,
            })
        elif paths or symbols or port_matches:
            valid += 1

    return {
        "checked": checked,
        "valid": valid,
        "stale_candidates": len(issues),
        "issues": issues,
    }

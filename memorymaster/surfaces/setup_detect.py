"""Pure, side-effect-free environment probing for MemoryMaster onboarding.

All probes are read-only, shell=False, timeout-bounded (<=5 s each), and
degrade gracefully to absent on ANY error — they never raise.

Public API
----------
detect_environment(*, cwd=None) -> Detected
format_plan(d, *, want_full_stack) -> list[str]
"""
from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from memorymaster.recall.qdrant_transport import QdrantTransportConfig


# ---------------------------------------------------------------------------
# DTO
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Detected:
    python_version: str
    pip_ok: bool
    os: str  # "Windows" | "Linux" | "Darwin"
    docker: bool  # `docker --version` ok
    docker_compose: bool  # `docker compose version` ok
    ollama: bool  # HTTP GET OLLAMA_URL/api/tags ok OR `ollama --version`
    ollama_models: tuple[str, ...]
    qdrant: bool  # HTTP GET QDRANT_URL/healthz ok
    obsidian_vault: Optional[str]  # path if an obsidian-vault/ found under cwd
    gitnexus: bool  # `.gitnexus/` present OR `npx gitnexus` resolvable
    claude_code: bool  # ~/.claude/ exists
    codex: bool  # ~/.codex/ exists
    mm_installed: bool  # `import memorymaster` works
    mm_mcp_registered: bool  # memorymaster already in ~/.claude.json mcpServers
    existing_hooks: tuple[str, ...]  # memorymaster hook names already in settings.json


# ---------------------------------------------------------------------------
# Internal probe helpers — every one returns a value, never raises
# ---------------------------------------------------------------------------

_PROBE_TIMEOUT = 5  # seconds


class _QdrantProbeUrl(str):
    """String-compatible marker that scopes Qdrant transport credentials."""


def _run(args: list[str]) -> Optional[str]:
    """Run a subprocess (shell=False, timeout<=5s).

    Returns stdout text on success, None on any failure.
    """
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            shell=False,
            timeout=_PROBE_TIMEOUT,
        )
        if result.returncode == 0:
            return result.stdout
        return None
    except Exception:  # noqa: BLE001 — intentional catch-all for probe
        return None


def _http_get(url: str) -> Optional[bytes]:
    """HTTP GET with timeout. Returns body bytes on 2xx, None otherwise."""
    try:
        target: str | urllib.request.Request = url
        if isinstance(url, _QdrantProbeUrl):
            transport = QdrantTransportConfig.from_env()
            target = transport.request(str(url), method="GET")
            req = transport.open(target, timeout=_PROBE_TIMEOUT)
        else:
            req = urllib.request.urlopen(target, timeout=_PROBE_TIMEOUT)  # noqa: S310
        return req.read()
    except Exception:  # noqa: BLE001
        return None


def _probe_python_version() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"


def _probe_pip_ok() -> bool:
    return _run([sys.executable, "-m", "pip", "--version"]) is not None


def _probe_docker() -> bool:
    return _run(["docker", "--version"]) is not None


def _probe_docker_compose() -> bool:
    return _run(["docker", "compose", "version"]) is not None


def _probe_ollama() -> tuple[bool, tuple[str, ...]]:
    """Return (reachable, model_names).

    Tries HTTP first (OLLAMA_URL env or default localhost:11434), then CLI.
    """
    ollama_url = os.environ.get("OLLAMA_URL", "http://localhost:11434")
    tags_url = ollama_url.rstrip("/") + "/api/tags"

    body = _http_get(tags_url)
    if body is not None:
        try:
            data = json.loads(body)
            models = tuple(
                m.get("name", "") for m in data.get("models", []) if m.get("name")
            )
            return True, models
        except Exception:  # noqa: BLE001
            return True, ()

    # Fallback: CLI probe
    if _run(["ollama", "--version"]) is not None:
        return True, ()

    return False, ()


def _probe_qdrant() -> bool:
    qdrant_url = os.environ.get("QDRANT_URL", "http://localhost:6333")
    healthz_url = qdrant_url.rstrip("/") + "/healthz"
    return _http_get(_QdrantProbeUrl(healthz_url)) is not None


def _probe_obsidian_vault(cwd: Path) -> Optional[str]:
    candidate = cwd / "obsidian-vault"
    if candidate.is_dir():
        return str(candidate)
    return None


def _probe_gitnexus(cwd: Path) -> bool:
    if (cwd / ".gitnexus").is_dir():
        return True
    return _run(["npx", "gitnexus", "--version"]) is not None


def _probe_claude_code(home: Path) -> bool:
    return (home / ".claude").is_dir()


def _probe_codex(home: Path) -> bool:
    return (home / ".codex").is_dir()


def _probe_mm_installed() -> bool:
    try:
        from importlib.util import find_spec

        spec = find_spec("memorymaster")
        return spec is not None
    except Exception:  # noqa: BLE001
        return False


def _probe_mm_mcp_registered(home: Path) -> bool:
    """Check ~/.claude.json for a memorymaster entry in mcpServers."""
    claude_json = home / ".claude.json"
    if not claude_json.is_file():
        return False
    try:
        data = json.loads(claude_json.read_text(encoding="utf-8"))
        servers = data.get("mcpServers", {})
        if isinstance(servers, dict):
            return any("memorymaster" in k for k in servers)
        return False
    except Exception:  # noqa: BLE001
        return False


def _probe_existing_hooks(home: Path) -> tuple[str, ...]:
    """Return hook event names that already contain a memorymaster entry."""
    settings_path = home / ".claude" / "settings.json"
    if not settings_path.is_file():
        return ()
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        hooks_section = data.get("hooks", {})
        if not isinstance(hooks_section, dict):
            return ()
        found: list[str] = []
        for event, entries in hooks_section.items():
            raw = json.dumps(entries)
            if "memorymaster" in raw:
                found.append(event)
        return tuple(found)
    except Exception:  # noqa: BLE001
        return ()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def detect_environment(*, cwd: Optional[Path] = None) -> Detected:
    """Probe the environment and return a frozen Detected snapshot.

    All probes are read-only, shell=False, timeout-bounded, and never raise.
    """
    effective_cwd = cwd if cwd is not None else Path.cwd()
    home = Path.home()

    ollama_ok, ollama_models = _probe_ollama()

    return Detected(
        python_version=_probe_python_version(),
        pip_ok=_probe_pip_ok(),
        os=platform.system(),
        docker=_probe_docker(),
        docker_compose=_probe_docker_compose(),
        ollama=ollama_ok,
        ollama_models=ollama_models,
        qdrant=_probe_qdrant(),
        obsidian_vault=_probe_obsidian_vault(effective_cwd),
        gitnexus=_probe_gitnexus(effective_cwd),
        claude_code=_probe_claude_code(home),
        codex=_probe_codex(home),
        mm_installed=_probe_mm_installed(),
        mm_mcp_registered=_probe_mm_mcp_registered(home),
        existing_hooks=_probe_existing_hooks(home),
    )


# ---------------------------------------------------------------------------
# Plan formatter
# ---------------------------------------------------------------------------

_STEP_WILL_DO = "will-do"
_STEP_PRESENT = "skip-present"
_STEP_CANT = "cant-missing"


def _line(tag: str, description: str) -> str:
    icons = {
        _STEP_WILL_DO: "[will-do]",
        _STEP_PRESENT: "[skip-present]",
        _STEP_CANT: "[cant-missing]",
    }
    return f"  {icons[tag]}  {description}"


def format_plan(d: Detected, *, want_full_stack: bool) -> list[str]:
    """Return a human-readable list of 'will-do / skip-present / cant-missing' lines.

    Each line describes one setup action and whether it will be applied,
    skipped (already present), or blocked by a missing dependency.
    """
    lines: list[str] = []

    # --- pip / memorymaster install ---
    if d.mm_installed:
        lines.append(_line(_STEP_PRESENT, "memorymaster already installed"))
    else:
        lines.append(_line(_STEP_WILL_DO, "pip install memorymaster"))

    # --- Claude Code hooks ---
    if d.claude_code:
        if d.existing_hooks:
            joined = ", ".join(sorted(d.existing_hooks))
            lines.append(
                _line(_STEP_PRESENT, f"Claude Code hooks already registered ({joined})")
            )
        else:
            lines.append(_line(_STEP_WILL_DO, "install Claude Code hooks (~/.claude/)"))
    else:
        lines.append(
            _line(_STEP_CANT, "Claude Code hooks — ~/.claude/ not found (install Claude Code first)")
        )

    # --- MCP registration ---
    if d.mm_mcp_registered:
        lines.append(_line(_STEP_PRESENT, "MCP server already registered in ~/.claude.json"))
    elif d.claude_code:
        lines.append(_line(_STEP_WILL_DO, "register MCP server in ~/.claude.json"))
    else:
        lines.append(
            _line(_STEP_CANT, "MCP registration — ~/.claude/ not found")
        )

    # --- Codex integration ---
    if d.codex:
        lines.append(_line(_STEP_PRESENT, "Codex (~/.codex/) present — will wire MCP + instructions"))
    else:
        lines.append(_line(_STEP_CANT, "Codex integration — ~/.codex/ not found (optional)"))

    # --- Full-stack: Docker / Qdrant / Ollama ---
    if want_full_stack:
        if d.qdrant:
            lines.append(_line(_STEP_PRESENT, "Qdrant already reachable"))
        elif d.docker_compose:
            lines.append(_line(_STEP_WILL_DO, "docker compose up -d qdrant"))
        else:
            lines.append(
                _line(
                    _STEP_CANT,
                    "Qdrant — Docker Compose not found; running in SQLite-only mode. "
                    "Install Docker or point QDRANT_URL at an existing service for Qdrant "
                    "index maintenance; retrieval remains quarantined.",
                )
            )

        if d.ollama:
            lines.append(
                _line(
                    _STEP_PRESENT,
                    "Ollama already reachable"
                    + (f" (models: {', '.join(d.ollama_models)})" if d.ollama_models else ""),
                )
            )
        elif d.docker_compose:
            lines.append(_line(_STEP_WILL_DO, "docker compose up -d ollama"))
        else:
            lines.append(
                _line(
                    _STEP_CANT,
                    "Ollama — Docker Compose not found; local LLM auto-ingest is OFF. "
                    "Install Docker or point OLLAMA_URL at an existing service.",
                )
            )
    else:
        lines.append(_line(_STEP_CANT, "Full-stack (Qdrant + Ollama) skipped — not requested"))

    # --- Obsidian vault ---
    if d.obsidian_vault:
        lines.append(_line(_STEP_PRESENT, f"Obsidian vault found at {d.obsidian_vault}"))
    else:
        lines.append(_line(_STEP_WILL_DO, "create obsidian-vault/ skeleton"))

    # --- GitNexus ---
    if d.gitnexus:
        lines.append(_line(_STEP_PRESENT, "GitNexus index (.gitnexus/) already present"))
    else:
        lines.append(
            _line(_STEP_CANT, "GitNexus index — .gitnexus/ not found (run `npx gitnexus analyze` to enable)")
        )

    return lines

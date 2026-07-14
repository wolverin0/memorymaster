"""MemoryMaster setup — installs hooks, MCP config, steward cron, and Obsidian skills.

Usage (after pip install memorymaster):
    memorymaster-setup

Usage (from a clone):
    python scripts/setup-hooks.py   # shim that calls this module

Interactive prompts for:
    - LLM provider (google/openai/anthropic/ollama) + API key
    - Project root (where memorymaster.db lives)
    - Claude Code hooks (recall, classify, validate-wiki, session-start, auto-ingest, precompact;
      plus opt-in --pretooluse grep/glob recall-inject)
    - Codex AGENTS.md integration
    - CLAUDE.md global integration
    - Steward cron (every 6h)
    - Obsidian skills installation
"""
import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

try:
    from importlib.resources import files as _resource_files
except ImportError:  # pragma: no cover - Python < 3.9
    from importlib_resources import files as _resource_files  # type: ignore

from memorymaster.surfaces.setup_detect import Detected, detect_environment, format_plan

# Templates are shipped inside the package as importable resources.
TEMPLATES_DIR = Path(str(_resource_files("memorymaster") / "config_templates"))

# PROJECT_ROOT is where the user wants memorymaster.db to live. It is
# prompted interactively; default is the current working directory.
PROJECT_ROOT = Path.cwd()

# Detect platform paths
IS_WINDOWS = platform.system() == "Windows"
HOME = Path.home()
CLAUDE_DIR = HOME / ".claude"
CLAUDE_JSON = HOME / ".claude.json"
CODEX_DIR = HOME / ".codex"
PYTHON_EXE = sys.executable

SETUP_PROFILES: dict[str, tuple[str, ...]] = {
    "minimal": ("db", "mcp"),
    "semantic": ("db", "mcp", "provider", "vector_backend"),
    "team": ("db", "mcp"),
    "full-lab": (
        "db",
        "mcp",
        "recall_hook",
        "capture_hook",
        "provider",
        "steward",
        "vector_backend",
        "dashboard",
    ),
}


@dataclass(frozen=True, slots=True)
class ComponentResult:
    name: str
    status: str
    detail: str


def evaluate_setup_profile(
    profile: str, components: dict[str, ComponentResult]
) -> tuple[str, int]:
    required = SETUP_PROFILES[profile]
    statuses = [components[name].status for name in required]
    if "BLOCKED" in statuses:
        return "BLOCKED", 2
    if "PARTIAL" in statuses:
        return "PARTIAL", 3
    return "PASS", 0


def _provider_component(
    detected: Detected, applied: dict[str, Any], config: dict[str, str]
) -> ComponentResult:
    provider = config.get("provider", "").lower()
    stack = applied.get("full_stack", {})
    if provider == "ollama":
        if detected.ollama or stack.get("ollama") == "reused":
            return ComponentResult("provider", "PASS", "Ollama readiness probe passed")
        if stack.get("ollama") == "started":
            return ComponentResult("provider", "PARTIAL", "Ollama started; readiness not yet reprobed")
        return ComponentResult("provider", "BLOCKED", "Ollama is not reachable")
    key_env = {
        "google": "GEMINI_API_KEY",
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
    }.get(provider, "")
    if config.get("api_key") or (key_env and os.environ.get(key_env)):
        return ComponentResult(
            "provider", "PARTIAL", "credentials configured; paid provider was not called"
        )
    return ComponentResult("provider", "BLOCKED", f"{provider or 'remote'} provider is not configured")


def _setup_components(
    *,
    profile: str,
    detected: Detected,
    applied: dict[str, Any],
    verify: dict[str, Any],
    llm_config: dict[str, str],
    db_target: str | Path,
) -> dict[str, ComponentResult]:
    verify_status = str(verify.get("status", "FAIL"))
    db_status = "PASS" if verify_status == "PASS" else "PARTIAL" if verify_status == "PARTIAL" else "BLOCKED"
    db_detail = str(verify.get("detail", "database verification unavailable"))
    if profile == "team" and not str(db_target).lower().startswith(("postgres://", "postgresql://")):
        db_status = "BLOCKED"
        db_detail = "team profile requires a PostgreSQL DSN and disposable two-role verification"

    mcp_actions = (str(applied.get("mcp_claude", "")), str(applied.get("mcp_codex", "")))
    try:
        from memorymaster.surfaces import mcp_server as installed_mcp

        mcp_importable = installed_mcp.FastMCP is not None
    except Exception:
        mcp_importable = False
    mcp_ok = mcp_importable and any(
        value in {"present", "registered"} for value in mcp_actions
    )
    recall_ok = (CLAUDE_DIR / "hooks" / "memorymaster-recall.py").is_file()
    capture_ok = (CLAUDE_DIR / "hooks" / "memorymaster-session-end.py").is_file()
    cron_state = str(applied.get("cron", "blocked"))
    stack = applied.get("full_stack", {}) if isinstance(applied.get("full_stack"), dict) else {}
    qdrant = stack.get("qdrant")
    if detected.qdrant or qdrant == "reused":
        vector = ComponentResult("vector_backend", "PASS", "Qdrant readiness probe passed")
    elif qdrant == "started":
        vector = ComponentResult("vector_backend", "PARTIAL", "Qdrant started; authenticated/TLS readiness not reprobed")
    else:
        vector = ComponentResult("vector_backend", "BLOCKED", "governed Qdrant backend is unavailable")

    return {
        "db": ComponentResult("db", db_status, db_detail),
        "mcp": ComponentResult(
            "mcp",
            "PASS" if mcp_ok else "BLOCKED",
            "MCP registration and import contract verified" if mcp_ok else "no supported MCP client was registered",
        ),
        "recall_hook": ComponentResult(
            "recall_hook", "PASS" if recall_ok else "BLOCKED",
            "recall hook resource and installed file verified" if recall_ok else "recall hook unavailable",
        ),
        "capture_hook": ComponentResult(
            "capture_hook", "PASS" if capture_ok else "BLOCKED",
            "quiet capture hook resource and installed file verified" if capture_ok else "capture hook unavailable",
        ),
        "provider": _provider_component(detected, applied, llm_config),
        "steward": ComponentResult(
            "steward",
            "PASS" if cron_state == "configured" else "PARTIAL" if cron_state == "manual" else "BLOCKED",
            "steward schedule configured" if cron_state == "configured" else "manual cron action required" if cron_state == "manual" else "steward schedule unavailable",
        ),
        "vector_backend": vector,
        "dashboard": ComponentResult(
            "dashboard", "PARTIAL", "dashboard entrypoint is installed; HTTP readiness belongs to R3.4"
        ),
    }

# ---------------------------------------------------------------------------
# Non-interactive mode (set by main() when --yes is passed). When True,
# ask()/ask_yn() never call input() — they return the supplied default. This
# is what makes the installer scriptable + hermetic-testable.
# ---------------------------------------------------------------------------
NON_INTERACTIVE = False


def set_non_interactive(value: bool) -> None:
    """Toggle module-level non-interactive mode (no input() prompts)."""
    global NON_INTERACTIVE
    NON_INTERACTIVE = bool(value)


def _load_json_preserving(path: Path) -> dict:
    """Load a JSON object from *path*, returning ``{}`` when it's absent.

    If the file EXISTS but is malformed JSON, back it up to
    ``<name>.corrupt-<timestamp>.bak`` and warn BEFORE returning ``{}`` — so a
    later write never silently overwrites (and loses) a user's hand-edited
    config. If the backup itself can't be written we raise rather than wipe:
    losing the data in place is worse than a clear, actionable abort.
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        backup = path.with_name(f"{path.name}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}.bak")
        try:
            shutil.copy2(path, backup)
        except OSError as exc:
            raise RuntimeError(
                f"{path} is not valid JSON and a backup could not be written ({exc}); "
                "refusing to overwrite. Fix or move the file, then re-run."
            ) from exc
        print(f"  WARNING: {path} is not valid JSON — backed up to {backup} before rewriting.")
        return {}
    return data if isinstance(data, dict) else {}


def ask(prompt, default=""):
    """Prompt user for input. In non-interactive mode, returns the default."""
    if NON_INTERACTIVE:
        return default
    suffix = f" [{default}]" if default else ""
    result = input(f"  {prompt}{suffix}: ").strip()
    return result or default


def ask_yn(prompt, default=True):
    """Yes/no prompt. In non-interactive mode, returns the default."""
    if NON_INTERACTIVE:
        return default
    suffix = " [Y/n]" if default else " [y/N]"
    result = input(f"  {prompt}{suffix}: ").strip().lower()
    if not result:
        return default
    return result in ("y", "yes")


def banner(text):
    print(f"\n{'=' * 60}")
    print(f"  {text}")
    print(f"{'=' * 60}\n")


def replace_placeholder(content, project_root):
    """Replace __MEMORYMASTER_PROJECT_ROOT__ with actual path."""
    return content.replace("__MEMORYMASTER_PROJECT_ROOT__", str(project_root))


# ---------------------------------------------------------------------------
# 1. LLM Provider config
# ---------------------------------------------------------------------------
def setup_llm_provider():
    banner("LLM Provider for Session-End Distillation")
    print("  The quiet session-end hook uses a cheap LLM to distill key learnings.")
    print("  Supported: google (Gemini Flash Lite, ~free), openai, anthropic, ollama\n")

    provider = ask("Provider", "google")
    api_key = ""
    model = ""

    if provider in ("google", "gemini"):
        api_key = ask("GEMINI_API_KEY")
        model = ask("Model", "gemini-3.1-flash-lite-preview")
    elif provider == "openai":
        api_key = ask("OPENAI_API_KEY")
        model = ask("Model", "gpt-4o-mini")
    elif provider in ("anthropic", "claude"):
        api_key = ask("ANTHROPIC_API_KEY")
        model = ask("Model", "claude-haiku-4-5-20251001")
    elif provider == "ollama":
        model = ask("Model", "llama3.2:3b")
    else:
        print(f"  Unknown provider: {provider}, defaulting to google")
        provider = "google"

    return {"provider": provider, "api_key": api_key, "model": model}


# ---------------------------------------------------------------------------
# 2. Install hooks
# ---------------------------------------------------------------------------
def install_hooks(llm_config, include_pretooluse: bool = False):
    banner("Claude Code Hooks")

    hooks_dir = CLAUDE_DIR / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    # Copy hook templates with placeholder replacement
    for hook_file in (TEMPLATES_DIR / "hooks").glob("*.py"):
        content = hook_file.read_text(encoding="utf-8")
        content = replace_placeholder(content, PROJECT_ROOT)
        dest = hooks_dir / hook_file.name
        dest.write_text(content, encoding="utf-8")
        print(f"  Installed: {dest}")

    # Update settings.json with hooks config. A malformed pre-existing file is
    # backed up (never silently wiped) by _load_json_preserving.
    settings_path = CLAUDE_DIR / "settings.json"
    settings = _load_json_preserving(settings_path)

    # Add env vars
    env = settings.setdefault("env", {})
    env["MEMORYMASTER_LLM_PROVIDER"] = llm_config["provider"]
    if llm_config["api_key"]:
        key_name = {
            "google": "GEMINI_API_KEY",
            "gemini": "GEMINI_API_KEY",
            "openai": "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "claude": "ANTHROPIC_API_KEY",
        }.get(llm_config["provider"], "GEMINI_API_KEY")
        env[key_name] = llm_config["api_key"]
    if llm_config["model"]:
        env["MEMORYMASTER_LLM_MODEL"] = llm_config["model"]

    # P1 WAL-discipline flag (spec §5): hook processes are spawned fresh by
    # Claude Code with this settings env — mirror the operator's CURRENT
    # machine-level value so the recall/auto-ingest/dream-sync hooks switch
    # regimes together with everything else. When the operator never set it,
    # write nothing: the in-code default (OFF) governs, and a stale pinned
    # value here could silently override a later `setx` rollback.
    wal_flag = os.environ.get("MEMORYMASTER_WAL_DISCIPLINE")
    if wal_flag is not None:
        env["MEMORYMASTER_WAL_DISCIPLINE"] = wal_flag
        print(f"  MEMORYMASTER_WAL_DISCIPLINE={wal_flag} (mirrored into hook env)")

    # Add hooks
    hooks = settings.setdefault("hooks", {})

    # UserPromptSubmit — recall + classify (in that order)
    recall_hook = {
        "hooks": [{
            "type": "command",
            "command": f'python "{hooks_dir / "memorymaster-recall.py"}"',
            "timeout": 5
        }]
    }
    classify_hook = {
        "hooks": [{
            "type": "command",
            "command": f'python "{hooks_dir / "memorymaster-classify.py"}"',
            "timeout": 5
        }]
    }
    ups_hooks = hooks.setdefault("UserPromptSubmit", [])
    # Remove existing memorymaster hooks
    ups_hooks[:] = [h for h in ups_hooks if "memorymaster" not in json.dumps(h)]
    ups_hooks.append(recall_hook)
    ups_hooks.append(classify_hook)

    # PostToolUse — validate-wiki on Edit/Write
    validate_wiki_hook = {
        "matcher": "Edit|Write",
        "hooks": [{
            "type": "command",
            "command": f'python "{hooks_dir / "memorymaster-validate-wiki.py"}"',
            "timeout": 5
        }]
    }
    ptu_hooks = hooks.setdefault("PostToolUse", [])
    ptu_hooks[:] = [h for h in ptu_hooks if "memorymaster-validate-wiki" not in json.dumps(h)]
    ptu_hooks.append(validate_wiki_hook)

    # SessionStart — inject MemoryMaster context on startup/resume
    session_start_hook = {
        "matcher": "startup|resume",
        "hooks": [{
            "type": "command",
            "command": f'python "{hooks_dir / "memorymaster-session-start.py"}"',
            "timeout": 10
        }]
    }
    ss_hooks = hooks.setdefault("SessionStart", [])
    ss_hooks[:] = [h for h in ss_hooks if "memorymaster" not in json.dumps(h)]
    ss_hooks.append(session_start_hook)

    # SessionEnd — one distilled ingest pass, quiet and cursor/budget bounded.
    session_end_hook = {
        "hooks": [{
            "type": "command",
            "command": f'python "{hooks_dir / "memorymaster-session-end.py"}"',
            "timeout": 30
        }]
    }
    se_hooks = hooks.setdefault("SessionEnd", [])
    se_hooks[:] = [h for h in se_hooks if "memorymaster" not in json.dumps(h)]
    se_hooks.append(session_end_hook)

    # Stop — auto-ingest
    stop_hook = {
        "hooks": [{
            "type": "command",
            "command": f'python "{hooks_dir / "memorymaster-auto-ingest.py"}"',
            "timeout": 10
        }]
    }
    stop_hooks = hooks.setdefault("Stop", [])
    stop_hooks[:] = [h for h in stop_hooks if "memorymaster" not in json.dumps(h)]
    stop_hooks.append(stop_hook)

    # PreCompact — force save before context compaction (permanent context loss)
    precompact_hook = {
        "hooks": [{
            "type": "command",
            "command": f'python "{hooks_dir / "memorymaster-precompact.py"}"',
            "timeout": 15
        }]
    }
    precompact_hooks = hooks.setdefault("PreCompact", [])
    precompact_hooks[:] = [h for h in precompact_hooks if "memorymaster" not in json.dumps(h)]
    precompact_hooks.append(precompact_hook)

    # PreToolUse — recall-inject on Grep/Glob (OPT-IN, --pretooluse). The hook
    # template itself is additionally gated by MEMORYMASTER_PRETOOLUSE_RECALL=1
    # at runtime. Previously the template was copied to disk but never
    # registered anywhere (fresh-eyes audit 2026-07-01) — README's "opt-in
    # hook" silently required hand-editing settings.json.
    pretooluse_hooks = hooks.setdefault("PreToolUse", [])
    pretooluse_hooks[:] = [h for h in pretooluse_hooks if "memorymaster-pretooluse-recall" not in json.dumps(h)]
    if include_pretooluse:
        pretooluse_hooks.append({
            "matcher": "Grep|Glob",
            "hooks": [{
                "type": "command",
                "command": f'python "{hooks_dir / "memorymaster-pretooluse-recall.py"}"',
                "timeout": 5
            }]
        })

    settings_path.write_text(json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  Updated: {settings_path}")


# ---------------------------------------------------------------------------
# 3. MCP config
# ---------------------------------------------------------------------------
def _mcp_command_args() -> tuple[str, list[str]]:
    """Return the (command, args) pair for the MCP server entry.

    Uses the non-deprecated module path memorymaster.surfaces.mcp_server.
    The installed console-script entry point is ``memorymaster-mcp`` (see
    pyproject), but we register the explicit interpreter + module form so the
    registration is robust even when the script dir is not on PATH.
    """
    return PYTHON_EXE, ["-m", "memorymaster.surfaces.mcp_server"]


def _mcp_server_entry(db_path: str) -> dict[str, Any]:
    command, args = _mcp_command_args()
    return {
        "type": "stdio",
        "command": command,
        "args": args,
        "env": {
            "MEMORYMASTER_DEFAULT_DB": db_path,
            "MEMORYMASTER_WORKSPACE": str(PROJECT_ROOT),
            "MEMORYMASTER_MCP_AUTH_MODE": "local-trusted",
        },
    }


def install_mcp(*, force: bool = False, already_registered: bool = False):
    """Register the MemoryMaster MCP server in ~/.claude.json.

    Brownfield-safe: if an entry already exists, skip (no clobber) unless
    ``force`` is set. ``already_registered`` lets the caller pass the
    Detected report so we avoid touching the file when nothing would change.
    """
    banner("MemoryMaster MCP Server (Global)")

    # Read-merge-write. A malformed pre-existing .claude.json is backed up
    # (never silently wiped) by _load_json_preserving; absent -> {}.
    data = _load_json_preserving(CLAUDE_JSON)
    servers = data.setdefault("mcpServers", {})

    if "memorymaster" in servers and not force:
        if not ask_yn("memorymaster MCP already configured. Overwrite?", False):
            print("  Keeping existing MCP entry (brownfield) — pass --force to replace.")
            return

    db_path = str(PROJECT_ROOT / "memorymaster.db")
    servers["memorymaster"] = _mcp_server_entry(db_path)

    CLAUDE_JSON.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  Added memorymaster to {CLAUDE_JSON}")


# Marker-bounded block for the Codex config.toml MCP table. We do NOT parse
# the whole TOML (no writer in stdlib for 3.10); instead we manage an
# idempotent fenced block so re-runs replace cleanly and unknown content is
# preserved untouched.
_CODEX_MCP_BEGIN = "# >>> memorymaster mcp (managed by memorymaster-setup) >>>"
_CODEX_MCP_END = "# <<< memorymaster mcp (managed by memorymaster-setup) <<<"


def install_mcp_codex(*, force: bool = False):
    """Register the MCP server for Codex (~/.codex/config.toml).

    Codex reads MCP servers from ``[mcp_servers.<name>]`` TOML tables. We
    write a marker-bounded block so re-runs are idempotent and any
    pre-existing user TOML outside the block is preserved verbatim.
    """
    if not CODEX_DIR.exists():
        return
    banner("MemoryMaster MCP Server (Codex)")
    config_path = CODEX_DIR / "config.toml"
    existing = config_path.read_text(encoding="utf-8") if config_path.exists() else ""

    has_block = _CODEX_MCP_BEGIN in existing
    if has_block and not force:
        if not ask_yn("memorymaster MCP already in Codex config. Overwrite?", False):
            print("  Keeping existing Codex MCP entry (brownfield) — pass --force to replace.")
            return

    db_path = str(PROJECT_ROOT / "memorymaster.db")
    command, args = _mcp_command_args()
    args_toml = ", ".join(json.dumps(a) for a in args)
    block = (
        f"{_CODEX_MCP_BEGIN}\n"
        "[mcp_servers.memorymaster]\n"
        f"command = {json.dumps(command)}\n"
        f"args = [{args_toml}]\n"
        "[mcp_servers.memorymaster.env]\n"
        f"MEMORYMASTER_DEFAULT_DB = {json.dumps(db_path)}\n"
        f"MEMORYMASTER_WORKSPACE = {json.dumps(str(PROJECT_ROOT))}\n"
        'MEMORYMASTER_MCP_AUTH_MODE = "local-trusted"\n'
        f"{_CODEX_MCP_END}\n"
    )

    if has_block:
        before, _, rest = existing.partition(_CODEX_MCP_BEGIN)
        _, _, after = rest.partition(_CODEX_MCP_END)
        # Drop a single trailing newline left by the old block end marker.
        new_content = before.rstrip("\n") + "\n\n" + block + after.lstrip("\n")
    else:
        sep = "\n\n" if existing.strip() else ""
        new_content = existing.rstrip("\n") + sep + ("\n" if existing.strip() else "") + block
        if not existing.strip():
            new_content = block

    config_path.write_text(new_content, encoding="utf-8")
    print(f"  Registered memorymaster MCP in {config_path}")


# ---------------------------------------------------------------------------
# 4. Append to CLAUDE.md and AGENTS.md
# ---------------------------------------------------------------------------
def append_instructions():
    banner("Append MemoryMaster Instructions")

    # Claude global CLAUDE.md — only when ~/.claude exists (Claude Code
    # installed). Mirrors the Codex guard below; without it a from-zero box
    # (no Claude Code yet) crashes writing into a non-existent ~/.claude.
    claude_md = CLAUDE_DIR / "CLAUDE.md"
    marker = "## MemoryMaster (Cross-Session Memory)"

    if not CLAUDE_DIR.exists():
        print(f"  {CLAUDE_DIR} not found — skipping CLAUDE.md (install Claude Code, then re-run)")
    elif claude_md.exists() and marker in claude_md.read_text(encoding="utf-8"):
        print(f"  {claude_md} already has MemoryMaster section — skipping")
    elif ask_yn("Append MemoryMaster instructions to ~/.claude/CLAUDE.md?"):
        append = (TEMPLATES_DIR / "claude-md-append.md").read_text(encoding="utf-8")
        existing = claude_md.read_text(encoding="utf-8") if claude_md.exists() else ""
        claude_md.write_text(existing.rstrip() + "\n\n" + append, encoding="utf-8")
        print(f"  Appended to {claude_md}")

    # Codex AGENTS.md
    codex_agents = CODEX_DIR / "AGENTS.md"
    if codex_agents.exists() and marker in codex_agents.read_text(encoding="utf-8"):
        print(f"  {codex_agents} already has MemoryMaster section — skipping")
    elif CODEX_DIR.exists() and ask_yn("Append MemoryMaster instructions to ~/.codex/AGENTS.md?"):
        append = (TEMPLATES_DIR / "codex-agents-md-append.md").read_text(encoding="utf-8")
        existing = codex_agents.read_text(encoding="utf-8") if codex_agents.exists() else ""
        codex_agents.write_text(existing.rstrip() + "\n\n" + append, encoding="utf-8")
        print(f"  Appended to {codex_agents}")

    # Codex/generic BEAT-3 session-end automation (the non-Claude gap).
    # Claude gets session-end distilled ingest via its Stop hook; Codex has no
    # native Stop event, so we point the operator at the turnkey reference script
    # rather than auto-scheduling a daemon they didn't ask for. The script sets
    # source_agent + caps the batch at 3 and routes through service.ingest.
    if CODEX_DIR.exists():
        print("  Codex BEAT-3 (session-end distilled ingest) — no native Stop hook.")
        print("  Packaged command: memorymaster-session-end")
        print("    Wire it as a Codex notify/exit hook or run at session end:")
        print(
            f'    "{PYTHON_EXE}" -m memorymaster.surfaces.session_end_ingest '
            "--db <path>/memorymaster.db --transcript <rollout.jsonl> "
            "--source-agent codex-session --cwd <project>"
        )
        print("    It distills <=3 learnings, sets source_agent, never raw-INSERTs.")


# ---------------------------------------------------------------------------
# 5. Steward cron
# ---------------------------------------------------------------------------
def setup_steward_cron() -> str:
    banner("Steward Cycle (every 6 hours)")

    if not ask_yn("Set up automatic steward cycle every 6 hours?"):
        return "blocked"

    steward_script = str(CLAUDE_DIR / "hooks" / "memorymaster-steward-cycle.py")

    if IS_WINDOWS:
        try:
            result = subprocess.run(
                ["schtasks", "/create",
                 "/tn", "MemoryMasterSteward",
                 "/tr", f'"{PYTHON_EXE}" "{steward_script}"',
                 "/sc", "hourly", "/mo", "6", "/f"],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                print("  Created Windows scheduled task: MemoryMasterSteward (every 6h)")
                return "configured"
            else:
                print(f"  Failed to create task: {result.stderr}")
                return "blocked"
        except Exception as e:
            print(f"  Error: {e}")
            return "blocked"
    else:
        cron_line = f"0 */6 * * * {PYTHON_EXE} {steward_script} >> /var/log/memorymaster-steward.log 2>&1"
        print("  Add this to your crontab (crontab -e):")
        print(f"  {cron_line}")
        return "manual"


# ---------------------------------------------------------------------------
# 6. Obsidian skills
# ---------------------------------------------------------------------------
def install_obsidian_skills():
    banner("Obsidian Skills")

    if not ask_yn("Install Obsidian skills for Claude Code?"):
        return

    skills_dir = CLAUDE_DIR / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)

    try:
        tmp = Path("/tmp/obsidian-skills") if not IS_WINDOWS else Path(os.environ.get("TEMP", "/tmp")) / "obsidian-skills"
        if tmp.exists():
            shutil.rmtree(tmp)

        subprocess.run(
            ["git", "clone", "--depth", "1", "https://github.com/kepano/obsidian-skills.git", str(tmp)],
            capture_output=True, check=True
        )

        for skill in (tmp / "skills").iterdir():
            if skill.is_dir():
                dest = skills_dir / skill.name
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(skill, dest)
                print(f"  Installed: {skill.name}")

        shutil.rmtree(tmp, ignore_errors=True)
    except Exception as e:
        print(f"  Error installing: {e}")
        print("  You can install manually: git clone https://github.com/kepano/obsidian-skills.git")


# ---------------------------------------------------------------------------
# 7. Full-stack orchestration (Qdrant + Ollama via Docker Compose)
# ---------------------------------------------------------------------------
SQLITE_ONLY_MESSAGE = (
    "Running in SQLite-only mode. Qdrant index maintenance + local LLM auto-ingest are OFF.\n"
    "  Retrieval remains available through authoritative SQLite ranking. To enable index\n"
    "  maintenance or local LLMs, use --full-stack or QDRANT_URL / OLLAMA_URL."
)


def setup_full_stack(detected: Detected, *, interactive: bool, yes: bool, model: str = "") -> dict[str, Any]:
    """Bring up the optional index + local-LLM stack (Qdrant + Ollama).

    Brownfield: reuse already-healthy services. No-Docker fallback: print the
    SQLite-only degraded message and CONTINUE (never block core install).

    Returns a dict describing what happened (for --json), including a
    ``degraded`` boolean and a human ``message`` when degraded.
    """
    banner("Full-Stack (Qdrant + Ollama)")
    result: dict[str, Any] = {
        "qdrant": "absent",
        "ollama": "absent",
        "degraded": False,
        "message": "",
        "compose_run": False,
    }

    # Brownfield: already-healthy services are reused as-is.
    if detected.qdrant:
        result["qdrant"] = "reused"
        print("  Qdrant already reachable — reusing.")
    if detected.ollama:
        result["ollama"] = "reused"
        print("  Ollama already reachable — reusing.")

    qdrant_needed = not detected.qdrant
    ollama_needed = not detected.ollama

    if not (qdrant_needed or ollama_needed):
        print("  Full stack already healthy — nothing to do.")
        return result

    # No Docker Compose → degraded fallback (non-fatal).
    if not detected.docker_compose:
        result["degraded"] = True
        result["message"] = SQLITE_ONLY_MESSAGE
        print(f"  {SQLITE_ONLY_MESSAGE}")
        return result

    if interactive and not yes:
        if not ask_yn("Start Qdrant + Ollama via docker compose?", True):
            result["degraded"] = True
            result["message"] = SQLITE_ONLY_MESSAGE
            print("  Skipped full-stack at user request.")
            print(f"  {SQLITE_ONLY_MESSAGE}")
            return result

    compose_file = PROJECT_ROOT / "docker-compose.yml"
    if not compose_file.is_file():
        result["degraded"] = True
        result["message"] = (
            "No docker-compose.yml exists in the selected project. Installed-wheel "
            "setup does not ship deployment assets; configure authenticated services "
            "explicitly or run from a release bundle."
        )
        print(f"  {result['message']}")
        return result
    up_args = ["docker", "compose"]
    up_args += ["-f", str(compose_file)]
    services = [s for s, needed in (("qdrant", qdrant_needed), ("ollama", ollama_needed)) if needed]
    up_args += ["up", "-d", *services]

    try:
        proc = subprocess.run(up_args, capture_output=True, text=True, timeout=120)
        if proc.returncode == 0:
            result["compose_run"] = True
            for svc in services:
                result[svc] = "started"
            print(f"  docker compose up -d {' '.join(services)} — OK")
        else:
            result["degraded"] = True
            result["message"] = SQLITE_ONLY_MESSAGE
            print(f"  docker compose failed (rc={proc.returncode}); continuing degraded.")
            print(f"  {SQLITE_ONLY_MESSAGE}")
            return result
    except Exception as exc:  # noqa: BLE001 — stack failure must never block core install
        result["degraded"] = True
        result["message"] = SQLITE_ONLY_MESSAGE
        print(f"  docker compose error: {exc}; continuing degraded.")
        print(f"  {SQLITE_ONLY_MESSAGE}")
        return result

    # Best-effort model pull (non-fatal).
    if ollama_needed and model:
        try:
            subprocess.run(["ollama", "pull", model], capture_output=True, text=True, timeout=300)
            print(f"  ollama pull {model} — requested")
        except Exception as exc:  # noqa: BLE001
            print(f"  ollama pull {model} failed (non-fatal): {exc}")

    return result


# ---------------------------------------------------------------------------
# 8. Verify install (sentinel round-trip)
# ---------------------------------------------------------------------------
def verify_install(db_path: str | Path) -> dict[str, Any]:
    """Ingest a sentinel claim via the core service, then query it back.

    Returns {"status": "PASS"|"PARTIAL"|"FAIL", "detail": str, "mcp_note": str}.
    Never raises — a failure degrades to a FAIL/PARTIAL result.
    """
    banner("Verify Install")
    db_path = str(db_path)
    result: dict[str, Any] = {"status": "FAIL", "detail": "", "mcp_note": ""}

    try:
        from memorymaster.core.models import CitationInput
        from memorymaster.core.service import MemoryService
    except Exception as exc:  # noqa: BLE001
        result["detail"] = f"could not import core service: {exc}"
        print(f"  FAIL — {result['detail']}")
        return result

    sentinel = "memorymaster setup verification sentinel claim"
    try:
        svc = MemoryService(db_path)
        svc.init_db()
        svc.ingest(
            sentinel,
            [CitationInput(source="setup-verify", locator="verify_install")],
            scope="project:memorymaster-verify",
            source_agent="memorymaster-setup",
            idempotency_key="memorymaster-setup-verify-sentinel",
        )
        hits = svc.query(
            "verification sentinel",
            limit=10,
            include_candidates=True,
            scope_allowlist=["project:memorymaster-verify"],
        )
        round_tripped = any("sentinel" in (getattr(c, "text", "") or "") for c in hits)
    except Exception as exc:  # noqa: BLE001
        result["detail"] = f"round-trip failed: {exc}"
        print(f"  FAIL — {result['detail']}")
        return result

    if round_tripped:
        result["status"] = "PASS"
        result["detail"] = "sentinel claim ingested and recalled successfully"
        print("  PASS — sentinel claim ingested and recalled.")
    else:
        result["status"] = "PARTIAL"
        result["detail"] = "ingest succeeded but query did not return the sentinel"
        print("  PARTIAL — ingest OK but recall did not find the sentinel.")

    if detect_environment(cwd=PROJECT_ROOT).mm_mcp_registered:
        result["mcp_note"] = "MCP registered — restart your session to load it."
        print(f"  {result['mcp_note']}")

    return result


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------
def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="memorymaster-setup",
        description="Detect-first, idempotent MemoryMaster onboarding.",
    )
    p.add_argument("-y", "--yes", action="store_true", help="non-interactive; accept all defaults")
    p.add_argument(
        "--profile",
        choices=tuple(SETUP_PROFILES),
        default=None,
        help="verify a named setup profile (default: minimal for normal setup)",
    )
    p.add_argument("--db", help="path to memorymaster.db (overrides project-root default)")
    p.add_argument(
        "--provider",
        choices=["google", "openai", "anthropic", "ollama"],
        help="LLM provider for quiet session-end distillation",
    )
    p.add_argument("--api-key", help="API key for the chosen provider")
    p.add_argument("--model", help="LLM model id")
    p.add_argument("--project-root", help="directory where memorymaster.db lives")
    p.add_argument(
        "--full-stack",
        dest="full_stack",
        action="store_true",
        default=None,
        help="bring up Qdrant + Ollama (default)",
    )
    p.add_argument(
        "--no-full-stack",
        dest="full_stack",
        action="store_false",
        help="skip the vector + local-LLM stack",
    )
    p.add_argument("--no-cron", action="store_true", help="skip steward cron setup")
    p.add_argument("--no-obsidian-skills", action="store_true", help="skip Obsidian skills install")
    p.add_argument(
        "--codex",
        dest="codex",
        action="store_true",
        default=None,
        help="force Codex MCP + instructions wiring",
    )
    p.add_argument("--no-codex", dest="codex", action="store_false", help="skip Codex wiring")
    p.add_argument("--force", action="store_true", help="overwrite existing MCP entries")
    p.add_argument(
        "--pretooluse",
        action="store_true",
        help="also register the opt-in PreToolUse Grep/Glob recall-inject hook "
             "(runtime-gated by MEMORYMASTER_PRETOOLUSE_RECALL=1)",
    )
    p.add_argument("--verify-only", action="store_true", help="run only the verify round-trip and exit")
    p.add_argument("--json", action="store_true", help="emit machine-readable JSON result")
    return p


def _resolve_provider_config(args: argparse.Namespace) -> dict[str, str]:
    """Build the llm_config from flags, falling back to interactive prompts."""
    defaults = {
        "google": ("GEMINI_API_KEY", "gemini-3.1-flash-lite-preview"),
        "openai": ("OPENAI_API_KEY", "gpt-4o-mini"),
        "anthropic": ("ANTHROPIC_API_KEY", "claude-haiku-4-5-20251001"),
        "ollama": ("", "llama3.2:3b"),
    }
    if args.provider:
        _, default_model = defaults[args.provider]
        return {
            "provider": args.provider,
            "api_key": args.api_key or "",
            "model": args.model or default_model,
        }
    # No provider flag: prompt (honors NON_INTERACTIVE → returns defaults).
    cfg = setup_llm_provider()
    if args.api_key:
        cfg["api_key"] = args.api_key
    if args.model:
        cfg["model"] = args.model
    return cfg


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def _emit_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def main(argv: Optional[list[str]] = None) -> int:
    """Entry point. In --json mode, all human chatter is routed to stderr so
    that stdout contains ONLY the parseable JSON document."""
    args = build_arg_parser().parse_args(argv)
    if not args.json:
        rc, _payload = _run_main(args)
        return rc

    import contextlib

    with contextlib.redirect_stdout(sys.stderr):
        rc, payload = _run_main(args)
    if payload is not None:
        _emit_json(payload)
    return rc


def _run_main(args: argparse.Namespace) -> tuple[int, Optional[dict[str, Any]]]:
    global PROJECT_ROOT

    # Never prompt when stdout may be parsed (--json), when explicitly
    # non-interactive (--yes), or for a quick --verify-only check — stdin may
    # not be a tty (CI, `docker exec`, an agent), where input() raises EOFError.
    set_non_interactive(bool(args.yes) or bool(args.json) or bool(args.verify_only))
    profile = args.profile or "minimal"
    want_full_stack = (
        profile in {"semantic", "full-lab"}
        if args.full_stack is None
        else bool(args.full_stack)
    )

    # --verify-only short-circuits BEFORE any project-root prompt: it only needs
    # --db (defaulting to cwd/memorymaster.db) and must never block on input.
    if args.verify_only:
        vdb: str | Path
        if args.db and "://" in args.db:
            vdb = args.db
        else:
            vdb = Path(args.db).expanduser().resolve() if args.db else Path.cwd() / "memorymaster.db"
        verify = verify_install(vdb)
        if args.profile is None:
            rc = 0 if verify.get("status") == "PASS" else 3 if verify.get("status") == "PARTIAL" else 1
            return rc, ({"verify": verify} if args.json else None)
        detected = detect_environment(cwd=Path.cwd())
        components = _setup_components(
            profile=profile,
            detected=detected,
            applied={},
            verify=verify,
            llm_config=_resolve_provider_config(args),
            db_target=vdb,
        )
        status, rc = evaluate_setup_profile(profile, components)
        payload = {
            "verify": verify,
            "profile": {"name": profile, "status": status},
            "components": {name: asdict(result) for name, result in components.items()},
        }
        return rc, (payload if args.json else None)

    # --- Resolve project root (flag > prompt > cwd) ---
    if args.project_root:
        root_input = args.project_root
    else:
        banner("MemoryMaster Setup")
        root_input = ask("Project root (where memorymaster.db lives)", str(Path.cwd()))
    PROJECT_ROOT = Path(root_input).expanduser().resolve()
    PROJECT_ROOT.mkdir(parents=True, exist_ok=True)

    if args.db and "://" in args.db:
        db_path: str | Path = args.db
    else:
        db_path = Path(args.db).expanduser().resolve() if args.db else PROJECT_ROOT / "memorymaster.db"

    # --- Detect-first ---
    detected = detect_environment(cwd=PROJECT_ROOT)
    plan = format_plan(detected, want_full_stack=want_full_stack)
    banner("Detection + Plan")
    print(f"  Project root: {PROJECT_ROOT}")
    print(f"  Python: {PYTHON_EXE}")
    print()
    for line in plan:
        print(line)
    print()

    if not args.json and not ask_yn("Continue?"):
        return 0, None

    applied: dict[str, Any] = {}

    # --- Init DB if needed ---
    if isinstance(db_path, Path) and not db_path.exists():
        if not args.json:
            print("\n  Initializing database...")
        try:
            subprocess.run(
                # `--db` is a GLOBAL arg — it MUST precede the subcommand,
                # else argparse exits 2 (unrecognized arguments).
                [PYTHON_EXE, "-m", "memorymaster", "--db", str(db_path), "init-db"],
                check=True,
                # capture_output: the subprocess writes to the REAL stdout fd,
                # which contextlib.redirect_stdout (a sys.stdout swap) does NOT
                # cover — without this its "initialized db" line corrupts the
                # --json document on stdout.
                capture_output=True,
                text=True,
            )
            applied["db_init"] = True
        except Exception as exc:  # noqa: BLE001
            applied["db_init"] = f"error: {exc}"

    # --- LLM provider config ---
    llm_config = _resolve_provider_config(args)
    applied["provider"] = llm_config["provider"]

    # --- Claude Code hooks (idempotent remove-then-add; brownfield-safe) ---
    if detected.claude_code:
        install_hooks(llm_config, include_pretooluse=getattr(args, "pretooluse", False))
        applied["hooks"] = "installed"
    else:
        applied["hooks"] = "skipped (no ~/.claude/)"

    # --- MCP registration (skip-if-present unless --force) ---
    if detected.claude_code:
        install_mcp(force=args.force, already_registered=detected.mm_mcp_registered)
        applied["mcp_claude"] = "present" if (detected.mm_mcp_registered and not args.force) else "registered"
    else:
        applied["mcp_claude"] = "skipped (no ~/.claude/)"

    # --- Instructions (CLAUDE.md / AGENTS.md / Codex session-end) ---
    append_instructions()

    # --- Codex MCP wiring ---
    want_codex = detected.codex if args.codex is None else bool(args.codex)
    if want_codex and detected.codex:
        install_mcp_codex(force=args.force)
        applied["mcp_codex"] = "registered"
    else:
        applied["mcp_codex"] = "skipped"

    # --- Steward cron (references the Claude hook script; needs ~/.claude) ---
    if not args.no_cron and detected.claude_code:
        applied["cron"] = setup_steward_cron()
    else:
        applied["cron"] = "skipped" if args.no_cron else "skipped (no ~/.claude/)"

    # --- Obsidian skills (installed into ~/.claude/skills) ---
    if not args.no_obsidian_skills and detected.claude_code:
        install_obsidian_skills()
        applied["obsidian_skills"] = "attempted"
    else:
        applied["obsidian_skills"] = "skipped"

    # --- Full-stack orchestration ---
    degraded = False
    if want_full_stack:
        stack = setup_full_stack(
            detected, interactive=not args.yes, yes=bool(args.yes), model=llm_config.get("model", "")
        )
        applied["full_stack"] = stack
        degraded = bool(stack.get("degraded"))
    else:
        applied["full_stack"] = {"skipped": True}

    # --- Verify ---
    verify = verify_install(db_path)

    components = _setup_components(
        profile=profile,
        detected=detected,
        applied=applied,
        verify=verify,
        llm_config=llm_config,
        db_target=db_path,
    )
    profile_status, profile_rc = evaluate_setup_profile(profile, components)

    banner(f"Setup {profile_status}")
    print("  What actually happened (skips reflect what's installed on this box):")
    if detected.claude_code:
        print("    - Claude hooks (session-start + on-demand recall + session-end distill) — installed")
        print("    - MemoryMaster MCP — registered (memorymaster.surfaces.mcp_server)")
    else:
        print("    - Claude Code not detected — hooks + MCP SKIPPED. Install Claude")
        print("      Code, then re-run `memorymaster-setup` to wire them.")
    print(f"    - Codex MCP — {applied.get('mcp_codex')}")
    print(f"    - DB: {db_path}  |  verify: {verify.get('status')}")
    print(f"    - LLM provider: {llm_config['provider']}")
    if degraded:
        print("    - Stack: SQLite-only — Qdrant index maintenance + local LLM OFF")
    print()
    print("  Next steps:")
    if detected.claude_code or detected.codex:
        print("    1. Restart Claude Code / Codex sessions to load hooks + MCP")
    else:
        print("    1. Install Claude Code or Codex, then re-run memorymaster-setup")
    print("    2. Re-check anytime with: memorymaster-setup --verify-only")
    print()

    if args.json:
        payload = {
            "detected": asdict(detected),
            "planned": plan,
            "applied": applied,
            "verify": verify,
            "degraded": degraded,
            "profile": {"name": profile, "status": profile_status},
            "components": {name: asdict(result) for name, result in components.items()},
        }
        return profile_rc, payload
    return profile_rc, None


if __name__ == "__main__":
    raise SystemExit(main())

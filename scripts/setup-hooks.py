"""MemoryMaster setup script — installs hooks, MCP config, steward cron, and Obsidian skills.

Usage:
    python scripts/setup-hooks.py

Interactive prompts for:
    - LLM provider (google/openai/anthropic/ollama) + API key
    - Claude Code hooks (recall + auto-ingest)
    - Codex AGENTS.md integration
    - CLAUDE.md global integration
    - Steward cron (every 6h)
    - Obsidian skills installation
"""
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent
TEMPLATES_DIR = PROJECT_ROOT / "config-templates"

# Detect platform paths
IS_WINDOWS = platform.system() == "Windows"
HOME = Path.home()
CLAUDE_DIR = HOME / ".claude"
CLAUDE_JSON = HOME / ".claude.json"
CODEX_DIR = HOME / ".codex"
PYTHON_EXE = sys.executable


def ask(prompt, default=""):
    """Prompt user for input."""
    suffix = f" [{default}]" if default else ""
    result = input(f"  {prompt}{suffix}: ").strip()
    return result or default


def ask_yn(prompt, default=True):
    """Yes/no prompt."""
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
    banner("LLM Provider for Auto-Ingest Stop Hook")
    print("  The Stop hook uses a cheap LLM to extract learnings from each session.")
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
def install_hooks(llm_config):
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

    # Update settings.json with hooks config
    settings_path = CLAUDE_DIR / "settings.json"
    settings = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            settings = {}

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

    # Add hooks
    hooks = settings.setdefault("hooks", {})

    # UserPromptSubmit — recall
    recall_hook = {
        "hooks": [{
            "type": "command",
            "command": f'python "{hooks_dir / "memorymaster-recall.py"}"',
            "timeout": 5
        }]
    }
    ups_hooks = hooks.setdefault("UserPromptSubmit", [])
    # Remove existing memorymaster hooks
    ups_hooks[:] = [h for h in ups_hooks if "memorymaster" not in json.dumps(h)]
    ups_hooks.append(recall_hook)

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

    settings_path.write_text(json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  Updated: {settings_path}")


# ---------------------------------------------------------------------------
# 3. MCP config
# ---------------------------------------------------------------------------
def install_mcp():
    banner("MemoryMaster MCP Server (Global)")

    if not CLAUDE_JSON.exists():
        print(f"  {CLAUDE_JSON} not found — creating")
        CLAUDE_JSON.write_text("{}", encoding="utf-8")

    data = json.loads(CLAUDE_JSON.read_text(encoding="utf-8"))
    servers = data.setdefault("mcpServers", {})

    if "memorymaster" in servers:
        if not ask_yn("memorymaster MCP already configured. Overwrite?", False):
            return

    db_path = str(PROJECT_ROOT / "memorymaster.db")
    servers["memorymaster"] = {
        "type": "stdio",
        "command": PYTHON_EXE,
        "args": ["-m", "memorymaster.mcp_server"],
        "env": {
            "MEMORYMASTER_DEFAULT_DB": db_path,
            "MEMORYMASTER_WORKSPACE": str(PROJECT_ROOT),
        }
    }

    CLAUDE_JSON.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  Added memorymaster to {CLAUDE_JSON}")


# ---------------------------------------------------------------------------
# 4. Append to CLAUDE.md and AGENTS.md
# ---------------------------------------------------------------------------
def append_instructions():
    banner("Append MemoryMaster Instructions")

    # Claude global CLAUDE.md
    claude_md = CLAUDE_DIR / "CLAUDE.md"
    marker = "## MemoryMaster (Cross-Session Memory)"

    if claude_md.exists() and marker in claude_md.read_text(encoding="utf-8"):
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


# ---------------------------------------------------------------------------
# 5. Steward cron
# ---------------------------------------------------------------------------
def setup_steward_cron():
    banner("Steward Cycle (every 6 hours)")

    if not ask_yn("Set up automatic steward cycle every 6 hours?"):
        return

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
            else:
                print(f"  Failed to create task: {result.stderr}")
        except Exception as e:
            print(f"  Error: {e}")
    else:
        cron_line = f"0 */6 * * * {PYTHON_EXE} {steward_script} >> /var/log/memorymaster-steward.log 2>&1"
        print(f"  Add this to your crontab (crontab -e):")
        print(f"  {cron_line}")


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
# Main
# ---------------------------------------------------------------------------
def main():
    banner("MemoryMaster Setup")
    print("  This will configure hooks, MCP, steward cron, and optional integrations.")
    print(f"  Project root: {PROJECT_ROOT}")
    print(f"  Python: {PYTHON_EXE}")
    print()

    if not ask_yn("Continue?"):
        return

    # Init DB if needed
    db_path = PROJECT_ROOT / "memorymaster.db"
    if not db_path.exists():
        print("\n  Initializing database...")
        subprocess.run([PYTHON_EXE, "-m", "memorymaster", "init-db", "--db", str(db_path)], check=True)

    llm_config = setup_llm_provider()
    install_hooks(llm_config)
    install_mcp()
    append_instructions()
    setup_steward_cron()
    install_obsidian_skills()

    banner("Setup Complete!")
    print("  Restart all Claude Code / Codex sessions to apply changes.")
    print()
    print("  What's configured:")
    print("    - Recall hook (UserPromptSubmit) — injects relevant claims into each prompt")
    print("    - Auto-ingest hook (Stop) — extracts learnings via LLM after each response")
    print("    - MemoryMaster MCP — 21 tools available in all sessions")
    print("    - Steward cron — validates claims every 6 hours")
    print(f"    - LLM provider: {llm_config['provider']}")
    print()
    print("  Next steps:")
    print("    1. Restart Claude Code sessions")
    print("    2. Run: memorymaster --db memorymaster.db run-cycle")
    print("    3. Open Obsidian vault at: obsidian-vault/")
    print()


if __name__ == "__main__":
    main()

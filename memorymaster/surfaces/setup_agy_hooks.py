"""Install agy hooks into an explicitly supplied .gemini directory only.

Run ``python -m memorymaster.surfaces.setup_agy_hooks --help`` for lane usage.
No Gemini settings.json or default home target is used. Registers SessionStart, PreInvocation
(per-prompt recall) and Stop under the "memorymaster" entry only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

SCRIPT_NAME = "memorymaster-agy.py"


def _python_command(executable: str) -> str:
    # agy versions that split commands on spaces do not remove quotes. Keep
    # argv tokens space-free, including when the chosen interpreter has spaces.
    if any(char.isspace() for char in executable) and os.name == "nt":
        import ctypes

        buffer = ctypes.create_unicode_buffer(32768)
        length = ctypes.windll.kernel32.GetShortPathNameW(executable, buffer, len(buffer))
        if 0 < length < len(buffer):
            executable = buffer.value
    if any(char.isspace() or char in '\"\'&|;<>`$()' for char in executable):
        raise ValueError("Choose a Python executable without spaces or shell metacharacters")
    return executable


def _foreign_recall_owner(config: dict) -> str | None:
    """Key of another hook group whose PreInvocation handler already references memorymaster (a second one
    would inject recall twice per prompt)."""
    for key, group in config.items():
        if key != "memorymaster" and isinstance(group, dict) and "memorymaster" in json.dumps(group.get("PreInvocation", [])).lower():
            return key
    return None


def install_agy_hooks(*, gemini_config_dir: str | Path, project_root: str | Path,
                      db_path: str | Path, python_executable: str = sys.executable) -> dict[str, str]:
    """Idempotently add our named hooks under <gemini_config_dir>/config/hooks.json."""
    config_dir = Path(gemini_config_dir).resolve() / "config"
    hooks_path = config_dir / "hooks.json"
    config = json.loads(hooks_path.read_text(encoding="utf-8")) if hooks_path.exists() else {}
    if not isinstance(config, dict):
        raise ValueError("agy hooks.json must be an object; existing file was preserved")
    managed = config.setdefault("memorymaster", {})
    if not isinstance(managed, dict):
        raise ValueError("memorymaster hook must be an object; existing file was preserved")
    python = _python_command(python_executable)
    owner = _foreign_recall_owner(config)
    for event in ("SessionStart", "PreInvocation", "Stop"):
        entries = managed.get(event, [])
        if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
            raise ValueError(f"{event} must be an array of handlers; existing file was preserved")
        managed[event] = [entry for entry in entries
                          if str(entry.get("command", "")).split()[1:] != [SCRIPT_NAME, event]]
        if event == "PreInvocation" and owner:
            if not managed[event]:
                del managed[event]
            continue  # the other hook already recalls per prompt
        managed[event].append({"type": "command", "command": f"{python} {SCRIPT_NAME} {event}", "timeout": 10})
    managed["enabled"] = True
    template = Path(__file__).parents[1] / "config_templates" / "hooks" / SCRIPT_NAME
    content = template.read_text(encoding="utf-8").replace(
        "__MEMORYMASTER_PROJECT_ROOT_REPR__", repr(str(Path(project_root).resolve()))
    ).replace("__MEMORYMASTER_DB_PATH_REPR__", repr(str(Path(db_path).resolve())))
    config_dir.mkdir(parents=True, exist_ok=True)
    destination = config_dir / SCRIPT_NAME
    destination.write_text(content, encoding="utf-8")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=config_dir, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(json.dumps(config, indent=2) + "\n")
        os.replace(temporary, hooks_path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {"hooks": str(hooks_path), "adapter": str(destination),
            "pre_invocation": f"skipped: hook group '{owner}' already runs memorymaster recall on PreInvocation"
            if owner else "registered"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gemini-config-dir", required=True, help="Target .gemini root (hooks go in config/)")
    parser.add_argument("--project-root", required=True, help="Installed MemoryMaster source/package root")
    parser.add_argument("--db", required=True, help="Authoritative recall database (not opened by installer)")
    parser.add_argument("--python", default=sys.executable, dest="python_executable")
    args = parser.parse_args()
    result = install_agy_hooks(gemini_config_dir=args.gemini_config_dir, project_root=args.project_root,
                               db_path=args.db, python_executable=args.python_executable)
    if result["pre_invocation"] != "registered":
        print(result["pre_invocation"], file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

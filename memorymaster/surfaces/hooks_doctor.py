"""Inspect explicit CLI config directories and probe installed hook contracts."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile

CLIS = ("claude", "codex", "agy", "opencode")
INJECTION_EVENTS = {
    "claude": {"SessionStart", "UserPromptSubmit"},
    "codex": {"SessionStart", "UserPromptSubmit", "SubagentStart", "PostToolUse"},
    "agy": {"SessionStart", "PreInvocation", "PostInvocation"},
}


def validate_output(cli: str, event: str, output: str) -> tuple[bool, bool]:
    """Return (contract valid, nonempty injection); quiet capture is valid."""
    if not output.strip():
        return True, False
    try:
        data = json.loads(output)
    except ValueError:
        # Codex also delivers plain stdout for these events. A JSON-looking
        # malformed envelope is still an adapter error, not plain context.
        plain = (cli == "codex" and event in {"SessionStart", "UserPromptSubmit", "SubagentStart"}
                 and not output.lstrip().startswith(("{", "[")))
        return plain, plain
    if not isinstance(data, dict):
        return False, False
    if cli == "agy":
        if set(data) - {"injectSteps", "decision", "reason"}:
            return False, False
        steps = data.get("injectSteps", [])
        if not isinstance(steps, list):
            return False, False
        injected = False
        for step in steps:
            if not isinstance(step, dict) or len(step) != 1:
                return False, False
            key, value = next(iter(step.items()))
            # The available contract documents string variants. Do not certify
            # arbitrary nested systemMessage protojson without its schema.
            if key not in {"ephemeralMessage", "userMessage"}:
                return False, False
            if not isinstance(value, str):
                return False, False
            injected |= bool(value.strip())
        if "reason" in data and not isinstance(data["reason"], str):
            return False, False
        if "decision" in data and data["decision"] not in {"continue", "stop"}:
            return False, False
        return True, injected and event in INJECTION_EVENTS["agy"]
    if set(data) - {"hookSpecificOutput", "systemMessage", "continue", "stopReason", "decision", "reason", "suppressOutput"}:
        return False, False
    for key in ("continue", "suppressOutput"):
        if key in data and not isinstance(data[key], bool):
            return False, False
    for key in ("systemMessage", "stopReason", "reason"):
        if key in data and not isinstance(data[key], str):
            return False, False
    specific = data.get("hookSpecificOutput", {})
    if not isinstance(specific, dict):
        return False, False
    if specific and specific.get("hookEventName") != event:
        return False, False
    context = specific.get("additionalContext", "")
    if not isinstance(context, str):
        return False, False
    return True, bool(context.strip()) and event in INJECTION_EVENTS.get(cli, set())


def _commands(config: dict, cli: str):
    groups = [config.get("hooks", {})] if cli != "agy" else list(config.values())
    for group in groups:
        if not isinstance(group, dict) or group.get("enabled") is False:
            continue
        for event, entries in group.items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                for handler in entry.get("hooks", [entry]):
                    if not isinstance(handler, dict):
                        continue
                    command = handler.get("commandWindows") if os.name == "nt" else None
                    command = command or handler.get("command", "")
                    if isinstance(command, str) and "memorymaster" in command.lower():
                        yield event, command, handler.get("timeout", 10)


def _probe(cli: str, event: str, command: str, timeout) -> dict:
    result = {"cli": cli, "event": event, "command": command, "status": "FAIL",
              "valid_output": False, "injected": False, "probe": "disposable fixture"}
    try:
        if cli == "agy" and any(char in command for char in ('"', "'")):
            result["detail"] = "agy command splitting does not remove quotes; use a space-free command/module adapter"
            return result
        prefixes = ("cmd.exe /d /v:off /s /c ", "cmd.exe /d /s /c ")
        prefix = next((item for item in prefixes if command.lower().startswith(item)), None)
        if cli == "codex" and os.name == "nt" and prefix is not None:
            wrapped = command[len(prefix):]
            if not (wrapped.startswith('"') and wrapped.endswith('"')):
                result["detail"] = "unsupported Windows Codex command wrapper"
                return result
            # Codex passes this whole string to cmd.exe /C. Parse only the
            # installer's known wrapper, then keep the Python allowlist below.
            wrapped = re.sub(r"\^([&|<>^()])", r"\1", wrapped[1:-1])
            parts = shlex.split(wrapped, posix=True)
        else:
            parts = shlex.split(command, posix=os.name != "nt")
        parts = [part[1:-1] if part.startswith('"') and part.endswith('"') else part for part in parts]
        if (len(parts) < 2 or not Path(parts[0]).stem.lower().startswith("python")
                or any(part in {"&&", "||", ";", "|", ">", "&"} for part in parts)
                or parts[1].startswith("-") and not (parts[1] == "-m" and len(parts) > 2
                                                        and parts[2].startswith("memorymaster."))):
            result["detail"] = "unsupported command: fixture probe requires a Python script or MemoryMaster module"
            return result
        if parts[1] != "-m":
            parts[1] = str(Path(parts[1]).resolve())
        with tempfile.TemporaryDirectory(prefix="memorymaster-hooks-doctor-") as scratch:
            root = Path(scratch) / "fixture"
            root.mkdir()
            scratch = str(root)
            # Do not inherit provider credentials, activation flags or live DB/URL pointers.
            env = {key: value for key, value in os.environ.items()
                   if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT"}}
            env.update({"HOME": scratch, "USERPROFILE": scratch, "TMP": scratch, "TEMP": scratch,
                        "PYTHONPATH": str(Path(__file__).resolve().parents[2]), "PYTHONDONTWRITEBYTECODE": "1",
                        "PYTHONIOENCODING": "utf-8", "MEMORYMASTER_HOME": str(root / ".memorymaster"),
                        "MEMORYMASTER_DEFAULT_DB": str(root / "memorymaster.db"),
                        "MEMORYMASTER_CAPTURE_STATE_DB": str(root / "capture-control.db"),
                        "MEMORYMASTER_HOOK_RECALL_REMOTE": "0", "MEMORYMASTER_RECALL_DENSE": "0",
                        "MEMORYMASTER_LLM_PROVIDER": "none", "MEMORYMASTER_SCOPE_DEFAULT": "project:fixture"})
            transcript = root / "transcript.jsonl"
            transcript.write_text("", encoding="utf-8")
            payload = {"session_id": "memorymaster-doctor", "cwd": scratch,
                       "hook_event_name": event, "source": "startup",
                       "prompt": "MemoryMaster doctor fixture recall deterministic context",
                       "transcript_path": str(transcript), "stop_hook_active": False}
            if cli == "agy":
                payload = {"conversationId": "memorymaster-doctor", "workspacePaths": [scratch],
                           "transcriptPath": str(transcript), "modelName": "doctor-fixture",
                           "lastUserInput": payload["prompt"]}
            completed = subprocess.run(
                [parts[0], "-m", "memorymaster.surfaces.hook_probe",
                 json.dumps({"root": scratch, "args": parts[1:]})],
                input=json.dumps(payload), capture_output=True, text=True, encoding="utf-8",
                env=env, cwd=scratch, timeout=min(30, max(1, float(timeout))), check=False,
            )
            valid, injected = validate_output(cli, event, completed.stdout)
            result.update(returncode=completed.returncode, valid_output=valid, injected=injected,
                          fixture_memory_seen="MemoryMaster doctor fixture recall" in completed.stdout)
            if completed.returncode:
                result["detail"] = "hook failed or attempted access outside the disposable fixture"
            elif not valid:
                result["detail"] = "stdout does not match this CLI event contract"
            else:
                result["status"] = "PASS" if injected or event not in INJECTION_EVENTS.get(cli, set()) else "WARN"
                result["detail"] = "context emitted" if injected else "valid quiet output; no injection observed"
    except subprocess.TimeoutExpired:
        result["detail"] = "hook timed out"
    except (OSError, ValueError, TypeError):
        result["detail"] = "hook command could not be executed"
    return result


def doctor(config_dirs: dict[str, Path]) -> dict:
    rows = []
    for cli, directory in config_dirs.items():
        if cli == "opencode":
            plugins = sorted(path for pattern in ("plugins/*", "plugin/*") for path in directory.glob(pattern)
                             if path.is_file() and "memorymaster" in path.name.lower())
            rows.append({"cli": cli, "status": "WARN", "plugins": [path.name for path in plugins],
                         "detail": "plugin requires a real OpenCode runtime probe" if plugins else "no MemoryMaster plugin installed"})
            continue
        path = directory / ("settings.json" if cli == "claude" else "hooks.json")
        try:
            config = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
            if not isinstance(config, dict):
                raise ValueError("expected object")
            commands = list(_commands(config, cli))
            if cli == "codex" and (directory / "config.toml").exists():
                try:
                    import tomllib
                except ImportError:
                    rows.append({"cli": cli, "status": "WARN",
                                 "detail": "inline TOML probe requires Python 3.11+; JSON hooks still probed"})
                    rows.extend(_probe(cli, event, command, timeout) for event, command, timeout in commands)
                    continue
                inline = tomllib.loads((directory / "config.toml").read_text(encoding="utf-8-sig"))
                commands.extend(_commands(inline, cli))
            elif not path.exists():
                raise FileNotFoundError(path)
        except (OSError, ValueError, TypeError):
            rows.append({"cli": cli, "status": "FAIL", "detail": "config missing or invalid"})
            continue
        if not commands:
            rows.append({"cli": cli, "status": "WARN", "detail": "no enabled MemoryMaster hook commands installed"})
        rows.extend(_probe(cli, event, command, timeout) for event, command, timeout in commands)
        if commands and not any(row.get("injected") for row in rows if row["cli"] == cli):
            rows.append({"cli": cli, "status": "WARN", "detail": "no automatic memory injection observed"})
    statuses = {row["status"] for row in rows}
    return {"status": "FAIL" if "FAIL" in statuses else "WARN" if "WARN" in statuses else "PASS",
            "checks": rows, "acceptance": "fixture contracts only; real CLI delivery and hook trust remain unverified"}


def register_hooks_parser(subparsers):
    hooks = subparsers.add_parser("hooks", help="Inspect installed CLI hooks")
    commands = hooks.add_subparsers(dest="hooks_command", required=True)
    parser = commands.add_parser("doctor", help="Probe hooks with disposable synthetic payloads")
    for cli in CLIS:
        parser.add_argument(f"--{cli}-dir", type=Path, help="Explicit CLI config directory")


def handle_hooks(args, _service, parser, _effective_db):
    directories = {cli: getattr(args, cli + "_dir") for cli in CLIS if getattr(args, cli + "_dir") is not None}
    if not directories:
        parser.error("hooks doctor requires at least one explicit --claude-dir/--codex-dir/--agy-dir/--opencode-dir")
    report = doctor(directories)
    if args.json_output:
        print(json.dumps(report))
    else:
        for row in report["checks"]:
            print(f"{row['status']} {row['cli']} {row.get('event', '')}: {row['detail']}")
        print(report["acceptance"])
    return {"PASS": 0, "WARN": 3, "FAIL": 1}[report["status"]]

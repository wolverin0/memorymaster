"""Rebuild and audit the MemoryMaster scheduled tasks on a Windows PC from ``ops/windows-tasks/manifest.json``.

``memorymaster-setup windows-tasks`` has three modes:

* ``--plan`` (default): read the live state and print what ``--apply`` would create or replace.
* ``--apply``: create the runtimes (venvs from pinned requirements plus the memorymaster wheel), write the
  wrappers and non-secret configuration, and register the tasks from schtasks XML. Idempotent; the XML of a
  task it replaces is saved first.
* ``--check``: diff the live state against the manifest; exit 1 on any drift.

Secrets are never read from or written to the repo: the two MCP tokens come from an environment variable or
the value already in HKCU, and ``--apply`` stops before changing anything when one is missing.
Everything the repo does not own (operator-edited configuration, ``create_only`` files) is created when
absent and otherwise left alone; ``--check`` reports it as drift.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import ntpath
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from xml.sax.saxutils import escape

MANIFEST_RELPATH = "ops/windows-tasks/manifest.json"
ANCHOR_DATE = "2026-01-01"  # StartBoundary date: only the time of day is meaningful
ENV_PREFIX = "MEMORYMASTER_WINTASKS_"
VARS_FILE_NAME = "windows-tasks.vars.json"
_TOKEN = re.compile(r"\{([A-Z][A-Z0-9_]*)\}")
_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"

# (manifest key, XML element, value Task Scheduler assumes when the element is absent)
_SETTINGS = (
    ("disallow_start_on_batteries", "DisallowStartIfOnBatteries", "true"),
    ("stop_on_batteries", "StopIfGoingOnBatteries", "true"),
    ("execution_time_limit", "ExecutionTimeLimit", "PT72H"),
    ("hidden", "Hidden", "false"),
    ("multiple_instances", "MultipleInstancesPolicy", "IgnoreNew"),
    ("priority", "Priority", "7"),
    ("start_when_available", "StartWhenAvailable", "false"),
)

CREATE, UPDATE, UNCHANGED = "create", "update", "unchanged"
KEPT, BLOCKED, EXTERNAL, SECRET, OPTIONAL = "differs-kept", "blocked", "external-missing", "secret-missing", "optional-missing"
_NOT_DRIFT = {UNCHANGED, OPTIONAL}


class WindowsTasksError(Exception):
    """A condition the operator must fix; the CLI prints it and exits 2 without changing anything."""


# --------------------------------------------------------------------------- variables

def find_repo(explicit: str | None = None) -> Path:
    candidates = [Path(explicit)] if explicit else [Path(__file__).resolve().parents[2], Path.cwd()]
    for candidate in candidates:
        if (candidate / MANIFEST_RELPATH).is_file():
            return candidate
    raise WindowsTasksError(f"{MANIFEST_RELPATH} not found; pass --repo <memorymaster checkout>")


def load_manifest(repo: Path) -> dict[str, Any]:
    return json.loads((repo / MANIFEST_RELPATH).read_text(encoding="utf-8"))


def render(text: str, values: dict[str, str], declared: set[str] | None = None) -> str:
    """Replace ``{NAME}`` tokens. Unknown lowercase/other braces (f-strings, PowerShell) are left alone;
    a declared variable without a value raises."""

    def sub(match: re.Match) -> str:
        name = match.group(1)
        if name in values:
            return values[name]
        if declared is not None and name in declared:
            raise WindowsTasksError(
                f"variable {name} has no value: pass --var {name}=<value> or set {ENV_PREFIX}{name}"
            )
        return match.group(0)

    return _TOKEN.sub(sub, text)


def _render_json(node: Any, values: dict[str, str], declared: set[str]) -> Any:
    if isinstance(node, str):
        return render(node, values, declared)
    if isinstance(node, list):
        return [_render_json(item, values, declared) for item in node]
    if isinstance(node, dict):
        return {key: _render_json(item, values, declared) for key, item in node.items()}
    return node


def build_vars(
    manifest: dict[str, Any], repo: Path, env: dict[str, str], cli_vars: dict[str, str],
) -> tuple[dict[str, str], set[str]]:
    """Resolve the variable set: built-ins, manifest defaults, vars file, environment, command line."""
    home = env.get("USERPROFILE") or str(Path.home())
    local = env.get("LOCALAPPDATA") or ntpath.join(home, "AppData", "Local")
    base = {
        "HOME": home,
        "LOCALAPPDATA": local,
        "USERNAME": env.get("USERNAME") or ntpath.basename(home),
        "USERDOMAIN": env.get("USERDOMAIN") or env.get("COMPUTERNAME") or "",
        "REPO": str(repo),
        "REPO_FWD": str(repo).replace("\\", "/"),
        "PYAPPS": ntpath.dirname(str(repo)) if "\\" in str(repo) else str(repo.parent),
    }
    spec = manifest["variables"]
    declared = set(base) | set(spec["defaults"]) | set(spec["required"]) | {"PYTHON_DIR"}
    overrides: dict[str, str] = {}
    vars_file = Path(home) / ".memorymaster" / VARS_FILE_NAME
    if vars_file.is_file():
        overrides.update({str(k): str(v) for k, v in json.loads(vars_file.read_text(encoding="utf-8")).items()})
    overrides.update({k[len(ENV_PREFIX):]: v for k, v in env.items() if k.startswith(ENV_PREFIX) and v})
    overrides.update(cli_vars)
    unknown = sorted(set(overrides) - declared)
    if unknown:
        raise WindowsTasksError(f"unknown variable(s): {', '.join(unknown)}")
    if "REPO" in overrides:  # the workspace the tasks serve may differ from the checkout holding the manifest
        repo_text = overrides["REPO"]
        base["REPO"] = repo_text
        base["REPO_FWD"] = repo_text.replace("\\", "/")
        base["PYAPPS"] = ntpath.dirname(repo_text)
    values = dict(base)
    raw = {**spec["defaults"], **{k: v for k, v in overrides.items() if k not in base}}
    values.update({k: v for k, v in overrides.items() if k in base})
    for _ in range(8):  # defaults reference each other and PYTHON_DIR; iterate to a fixed point
        values["PYTHON_DIR"] = ntpath.dirname(values.get("PYTHON", ""))
        changed = False
        for name, template in raw.items():
            resolved = render(template, values)
            if values.get(name) != resolved:
                values[name], changed = resolved, True
        if not changed:
            break
    return values, declared


def make_redactor(values: dict[str, str], enabled: bool) -> Callable[[str], str]:
    if not enabled:
        return lambda text: text
    pairs = sorted(
        ((values[name], f"{{{name}}}") for name in ("HERMES_HOST", "REPO", "REPO_FWD", "PYAPPS", "LOCALAPPDATA", "HOME", "USERNAME")
         if len(values.get(name, "")) >= 4),
        key=lambda pair: -len(pair[0]),
    )

    def redact(text: str) -> str:
        for needle, token in pairs:
            text = text.replace(needle, token)
        return text

    return redact


# --------------------------------------------------------------------------- task XML

def effective_settings(manifest: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    spec = dict(task.get("settings", {}))
    merged = dict(manifest.get("settings_presets", {}).get(spec.pop("preset", ""), {}))
    merged.update(spec)
    return merged


def _bool(value: Any) -> str:
    return "true" if value is True else "false" if value is False else str(value)


def _trigger_xml(trigger: dict[str, Any], values: dict[str, str]) -> str:
    kind = trigger["type"]
    if kind == "logon":
        return f"<LogonTrigger><UserId>{escape(values['USERDOMAIN'])}\\{escape(values['USERNAME'])}</UserId></LogonTrigger>"
    start = f"<StartBoundary>{ANCHOR_DATE}T{trigger['at']}</StartBoundary>"
    if kind == "time":
        repetition = ""
        if "repetition" in trigger:
            rep = trigger["repetition"]
            repetition = "<Repetition>" + f"<Interval>{rep['interval']}</Interval>"
            if "duration" in rep:
                repetition += f"<Duration>{rep['duration']}</Duration>"
            if "stop_at_duration_end" in rep:
                repetition += f"<StopAtDurationEnd>{_bool(rep['stop_at_duration_end'])}</StopAtDurationEnd>"
            repetition += "</Repetition>"
        return f"<TimeTrigger>{start}{repetition}</TimeTrigger>"
    if kind == "daily":
        return f"<CalendarTrigger>{start}<ScheduleByDay><DaysInterval>{trigger.get('every', 1)}</DaysInterval></ScheduleByDay></CalendarTrigger>"
    if kind == "weekly":
        days = "".join(f"<{day} />" for day in trigger["days"])
        return (f"<CalendarTrigger>{start}<ScheduleByWeek><WeeksInterval>{trigger.get('every', 1)}</WeeksInterval>"
                f"<DaysOfWeek>{days}</DaysOfWeek></ScheduleByWeek></CalendarTrigger>")
    raise WindowsTasksError(f"unknown trigger type {kind!r}")


def task_xml(manifest: dict[str, Any], task: dict[str, Any], values: dict[str, str], declared: set[str]) -> str:
    def fill(text: str) -> str:
        return render(text, values, declared)

    settings = effective_settings(manifest, task)
    parts = []
    for key, element, _default in _SETTINGS[:2]:
        if key in settings:
            parts.append(f"<{element}>{_bool(settings[key])}</{element}>")
    for key, element, _default in _SETTINGS[2:]:
        if key in settings:
            parts.append(f"<{element}>{_bool(settings[key])}</{element}>")
        if key == "priority" and "restart_on_failure" in settings:  # schema order: ... Priority, RestartOnFailure
            rof = settings["restart_on_failure"]
            parts.append(f"<RestartOnFailure><Count>{rof['count']}</Count><Interval>{rof['interval']}</Interval></RestartOnFailure>")
    action = task["action"]
    exec_xml = f"<Command>{escape(fill(action['command']))}</Command>"
    if action.get("arguments"):
        exec_xml += f"<Arguments>{escape(fill(action['arguments']))}</Arguments>"
    if action.get("working_directory"):
        exec_xml += f"<WorkingDirectory>{escape(fill(action['working_directory']))}</WorkingDirectory>"
    triggers = "".join(_trigger_xml(t, values) for t in task["triggers"])
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        f'<Task version="1.3" xmlns="{_NS}">\n'
        f"<RegistrationInfo><Description>{escape(fill(task.get('description', '')))}</Description></RegistrationInfo>\n"
        f'<Principals><Principal id="Author"><UserId>{escape(values["USERDOMAIN"])}\\{escape(values["USERNAME"])}</UserId>'
        "<LogonType>InteractiveToken</LogonType></Principal></Principals>\n"
        f"<Settings>{''.join(parts)}</Settings>\n"
        f"<Triggers>{triggers}</Triggers>\n"
        f'<Actions Context="Author"><Exec>{exec_xml}</Exec></Actions>\n'
        "</Task>\n"
    )


def normalize_xml(text: str) -> dict[str, Any]:
    """Reduce a task definition to the fields the manifest controls, with Task Scheduler's defaults filled in.

    Ignored on purpose: registration date/author/URI/description, the principal's user id (a SID live, a
    name here), the date part of StartBoundary, idle settings and the unified-scheduling flag.
    """
    root = ET.fromstring(re.sub(r"^\s*<\?xml[^>]*\?>", "", text.lstrip("﻿")))
    for element in root.iter():
        element.tag = element.tag.rsplit("}", 1)[-1]

    def txt(path: str, default: str = "") -> str:
        found = root.find(path)
        return default if found is None or found.text is None or not found.text.strip() else found.text.strip()

    settings = {key: txt(f"Settings/{element}", default).lower() if default in ("true", "false") else txt(f"Settings/{element}", default)
                for key, element, default in _SETTINGS}
    rof = root.find("Settings/RestartOnFailure")
    settings["restart_on_failure"] = (
        {"count": txt("Settings/RestartOnFailure/Count"), "interval": txt("Settings/RestartOnFailure/Interval")}
        if rof is not None else None
    )
    triggers = []
    for node in root.find("Triggers") if root.find("Triggers") is not None else []:
        entry: dict[str, Any] = {"type": {"LogonTrigger": "logon", "TimeTrigger": "time"}.get(node.tag, node.tag)}
        start = (node.findtext("StartBoundary") or "").strip()
        if start:
            entry["at"] = re.split(r"[+\-Z]", start.split("T", 1)[-1], maxsplit=1)[0]
        rep = node.find("Repetition")
        if rep is not None:
            entry["repetition"] = {
                "interval": (rep.findtext("Interval") or "").strip(),
                "duration": (rep.findtext("Duration") or "").strip(),
                "stop_at_duration_end": (rep.findtext("StopAtDurationEnd") or "false").strip().lower(),
            }
        if node.tag == "CalendarTrigger":
            by_week = node.find("ScheduleByWeek")
            if by_week is not None:
                entry["type"] = "weekly"
                entry["every"] = (by_week.findtext("WeeksInterval") or "1").strip()
                entry["days"] = sorted(day.tag for day in by_week.iterfind("DaysOfWeek/*"))
            else:
                entry["type"] = "daily"
                entry["every"] = (node.findtext("ScheduleByDay/DaysInterval") or "1").strip()
        triggers.append(entry)
    return {
        "logon_type": txt("Principals/Principal/LogonType", "InteractiveToken"),
        "run_level": txt("Principals/Principal/RunLevel", "LeastPrivilege"),
        "settings": settings,
        "triggers": triggers,
        "command": txt("Actions/Exec/Command"),
        "arguments": txt("Actions/Exec/Arguments"),
        "working_directory": txt("Actions/Exec/WorkingDirectory"),
    }


def diff_normalized(live: Any, wanted: Any, path: str = "") -> list[str]:
    if isinstance(live, dict) and isinstance(wanted, dict):
        out: list[str] = []
        for key in sorted(set(live) | set(wanted)):
            out += diff_normalized(live.get(key), wanted.get(key), f"{path}.{key}" if path else key)
        return out
    if isinstance(live, list) and isinstance(wanted, list) and len(live) == len(wanted):
        out = []
        for index, (a, b) in enumerate(zip(live, wanted)):
            out += diff_normalized(a, b, f"{path}[{index}]")
        return out
    return [] if live == wanted else [f"{path}: live={live} manifest={wanted}"]


# --------------------------------------------------------------------------- host (schtasks, registry, pip)

class WindowsHost:
    """Everything that touches the machine outside the file system. Tests replace it."""

    def query_task_xml(self, name: str) -> str | None:
        done = subprocess.run(["schtasks", "/Query", "/TN", name, "/XML"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", check=False)
        return done.stdout if done.returncode == 0 and done.stdout.strip() else None

    def register_task(self, name: str, xml: str) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "task.xml"
            path.write_bytes(xml.encode("utf-16"))
            done = subprocess.run(["schtasks", "/Create", "/TN", name, "/XML", str(path), "/F"],
                                  capture_output=True, text=True, check=False)
        if done.returncode != 0:
            raise WindowsTasksError(f"schtasks /Create {name} failed: {done.stderr.strip() or done.stdout.strip()}")

    def registry_get(self, key: str, name: str) -> str | None:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
                value, _kind = winreg.QueryValueEx(handle, name)
        except OSError:
            return None
        return value if isinstance(value, str) and value.strip() else None

    def registry_set(self, key: str, name: str, value: str) -> None:
        import winreg

        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as handle:
            winreg.SetValueEx(handle, name, 0, winreg.REG_SZ, value)

    def run(self, argv: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(argv, capture_output=True, text=True, check=False)

    def freeze(self, python: str) -> dict[str, str]:
        done = self.run([python, "-I", "-m", "pip", "freeze", "--all"])
        return parse_requirements(done.stdout) if done.returncode == 0 else {}

    def package_version(self, python: str, package: str) -> str | None:
        done = self.run([python, "-I", "-c", f"import importlib.metadata as m; print(m.version({package!r}))"])
        return done.stdout.strip() if done.returncode == 0 and done.stdout.strip() else None

    def build_wheel(self, python: str, repo: Path, out_dir: Path) -> Path:
        done = self.run([python, "-m", "pip", "wheel", "--no-deps", "-w", str(out_dir), str(repo)])
        wheels = sorted(out_dir.glob("memorymaster-*.whl"))
        if done.returncode != 0 or not wheels:
            raise WindowsTasksError(f"could not build the memorymaster wheel: {done.stderr.strip()[-300:]}")
        return wheels[-1]


class PlanOnlyHost:
    """Used by --plan off Windows: no live state exists, so every item is reported as 'create'."""

    def query_task_xml(self, name: str) -> str | None:
        return None

    def registry_get(self, key: str, name: str) -> str | None:
        return None

    def freeze(self, python: str) -> dict[str, str]:
        return {}

    def package_version(self, python: str, package: str) -> str | None:
        return None


def _canon(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_requirements(text: str) -> dict[str, str]:
    pins = {}
    for line in text.splitlines():
        match = re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+)\s*$", line)
        if match:
            pins[_canon(match.group(1))] = match.group(2)
    return pins


def repo_version(repo: Path) -> str:
    match = re.search(r'^version\s*=\s*"([^"]+)"', (repo / "pyproject.toml").read_text(encoding="utf-8"), re.M)
    return match.group(1) if match else ""


# --------------------------------------------------------------------------- reconcile

def _item(kind: str, target: str, status: str, detail: str = "", **extra: Any) -> dict[str, Any]:
    return {"kind": kind, "target": target, "status": status, "detail": detail, **extra}


def _same_text(a: str, b: str) -> bool:
    def norm(text: str) -> str:
        return text.lstrip("﻿").replace("\r\n", "\n").rstrip("\n")

    return norm(a) == norm(b)


def _selected_tasks(manifest: dict[str, Any], only: list[str]) -> list[dict[str, Any]]:
    names = {task["name"] for task in manifest["tasks"]}
    unknown = sorted(set(only) - names)
    if unknown:
        raise WindowsTasksError(f"unknown task(s) for --only: {', '.join(unknown)}")
    return [task for task in manifest["tasks"] if not only or task["name"] in only]


def _blocked_reason(repo: Path, task: dict[str, Any]) -> str:
    missing = [rel for rel in task.get("requires_repo_files", []) if not (repo / rel).exists()]
    return f"repo file(s) missing: {', '.join(missing)}" if missing else ""


class Reconciler:
    def __init__(self, repo: Path, manifest: dict[str, Any], values: dict[str, str], declared: set[str],
                 host: Any, env: dict[str, str], only: list[str], apply: bool,
                 wheel: Path | None = None, wheel_sha256: str = "") -> None:
        self.repo, self.manifest, self.values, self.declared = repo, manifest, values, declared
        self.host, self.env, self.apply = host, env, apply
        self.wheel, self.wheel_sha256 = wheel, wheel_sha256.lower()
        self.only = only
        self.tasks = _selected_tasks(manifest, only)
        self.blocked = {t["name"]: _blocked_reason(repo, t) for t in self.tasks if _blocked_reason(repo, t)}
        self.live_tasks = [t for t in self.tasks if t["name"] not in self.blocked]
        self.stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.backup_dir = Path(values["RUNTIME"]) / "task-backups"
        self._wheel_ready: Path | None = None
        self._freeze: dict[str, dict[str, str]] = {}

    def fill(self, text: str) -> str:
        return render(text, self.values, self.declared)

    # ---- orchestration
    def run(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for task in self.tasks:
            if task["name"] in self.blocked:
                items.append(_item("task", task["name"], BLOCKED, self.blocked[task["name"]]))
        secrets = self.registry_items()  # evaluated first: a missing secret must stop --apply before any write
        if self.apply and any(i["status"] == SECRET for i in secrets):
            raise WindowsTasksError(
                "missing secret(s); nothing was changed:\n" + "\n".join(
                    f"  - {i['target']}: {i['detail']}" for i in secrets if i["status"] == SECRET))
        items += self.runtime_items()
        items += self.file_items()
        items += self.apply_registry(secrets)
        items += self.task_items()
        return items

    # ---- runtimes
    def _runtime_names(self) -> list[str]:
        wanted: list[str] = []
        for task in self.live_tasks:
            for name in task.get("runtimes", []):
                if name not in wanted:
                    wanted.append(name)
        return wanted

    def _python_of(self, runtime: dict[str, Any]) -> str:
        if runtime["kind"] == "system":
            return self.fill(runtime["python"])
        return ntpath.join(self.fill(runtime["path"]), "Scripts", "python.exe")

    def _pins_missing(self, python: str, reqs: Path) -> list[str]:
        have = self._freeze.setdefault(python, self.host.freeze(python))
        return [f"{name}=={ver}" for name, ver in parse_requirements(reqs.read_text(encoding="utf-8")).items()
                if have.get(name) != ver]

    def _get_wheel(self) -> Path:
        if self._wheel_ready is None:
            wheel = self.wheel
            if wheel is None:
                out = Path(tempfile.mkdtemp(prefix="mm-wheel-"))
                wheel = self.host.build_wheel(self.fill("{PYTHON}"), self.repo, out)
            if self.wheel_sha256:
                digest = hashlib.sha256(Path(wheel).read_bytes()).hexdigest()
                if digest != self.wheel_sha256:
                    raise WindowsTasksError(f"wheel hash mismatch: {Path(wheel).name} is not the reviewed build")
            self._wheel_ready = Path(wheel)
        return self._wheel_ready

    def runtime_items(self) -> list[dict[str, Any]]:
        items = []
        for name in self._runtime_names():
            runtime = self.manifest["runtimes"][name]
            kind = runtime["kind"]
            if kind == "dir":
                path = Path(self.fill(runtime["path"]))
                if path.is_dir():
                    items.append(_item("runtime", name, UNCHANGED, "directory exists"))
                else:
                    if self.apply:
                        path.mkdir(parents=True, exist_ok=True)
                    items.append(_item("runtime", name, CREATE, "wrapper directory"))
                continue
            python = self._python_of(runtime)
            exists = Path(python).is_file()
            if kind == "system" and not exists:
                items.append(_item("runtime", name, EXTERNAL, "install Python 3.12 first, then re-run"))
                continue
            reqs = self.repo / runtime["requirements"]
            problems: list[str] = []
            if not exists:
                problems.append("venv absent")
            else:
                problems += [f"pin {pin}" for pin in self._pins_missing(python, reqs)]
                if runtime.get("wheel") and self.host.package_version(python, "memorymaster") != repo_version(self.repo):
                    problems.append(f"memorymaster {repo_version(self.repo)} not installed")
            if not problems:
                items.append(_item("runtime", name, UNCHANGED, "pins and wheel match"))
                continue
            if self.apply:
                if not exists:
                    self._must(self.host.run([self.fill("{PYTHON}"), "-m", "venv", self.fill(runtime["path"])]), f"create venv {name}")
                self._must(self.host.run([python, "-I", "-m", "pip", "install", "-r", str(reqs)]), f"pip install pins into {name}")
                if runtime.get("wheel"):
                    self._must(self.host.run([python, "-I", "-m", "pip", "install", "--no-deps", "--force-reinstall",
                                              str(self._get_wheel())]), f"install wheel into {name}")
                self._freeze.pop(python, None)
            shown = ", ".join(problems[:4]) + (f" (+{len(problems) - 4} more)" if len(problems) > 4 else "")
            items.append(_item("runtime", name, UPDATE if exists else CREATE, shown))
        return items

    @staticmethod
    def _must(done: subprocess.CompletedProcess, what: str) -> None:
        if done.returncode != 0:
            raise WindowsTasksError(f"{what} failed: {(done.stderr or done.stdout).strip()[-400:]}")

    # ---- files
    def file_items(self) -> list[dict[str, Any]]:
        items = []
        live_names = {t["name"] for t in self.live_tasks}
        for entry in self.manifest["files"]:
            if entry["task"] not in live_names:
                continue
            dest = Path(self.fill(entry["dest"]))
            if "content_json" in entry:
                wanted_obj = _render_json(entry["content_json"], self.values, self.declared)
                wanted = json.dumps(wanted_obj, indent=2) + "\n"
            else:
                wanted_obj = None
                wanted = self.fill((self.repo / entry["source"]).read_bytes().decode("utf-8-sig"))
            if not dest.is_file():
                status, detail = CREATE, "missing"
            else:
                current = dest.read_bytes().decode("utf-8-sig", errors="replace")
                try:
                    same = json.loads(current) == wanted_obj if wanted_obj is not None else _same_text(current, wanted)
                except ValueError:
                    same = False
                if same:
                    items.append(_item("file", entry["id"], UNCHANGED, str(dest)))
                    continue
                status = UPDATE if entry["policy"] == "managed" else KEPT
                detail = "content differs" + ("" if status == UPDATE else " (operator-owned, kept)")
            if self.apply and status in (CREATE, UPDATE):
                dest.parent.mkdir(parents=True, exist_ok=True)
                if status == UPDATE:
                    self.backup_dir.mkdir(parents=True, exist_ok=True)
                    (self.backup_dir / f"{dest.name}.{self.stamp}.bak").write_bytes(dest.read_bytes())
                dest.write_bytes(wanted.encode("utf-8"))
            items.append(_item("file", entry["id"], status, f"{detail}: {dest}"))
        return items

    # ---- registry, secrets, user environment
    def registry_items(self) -> list[dict[str, Any]]:
        """Evaluate (never write) the secrets; plain values are handled by apply_registry."""
        items = []
        live_names = {t["name"] for t in self.live_tasks}
        for block in self.manifest["registry"]:
            if block["task"] not in live_names:
                continue
            for secret in block["secrets"]:
                target = f"HKCU\\{block['key']}\\{secret['name']}"
                if self.host.registry_get(block["key"], secret["name"]):
                    items.append(_item("secret", target, UNCHANGED, "present (value not read out)"))
                elif self.env.get(secret["env"]):
                    items.append(_item("secret", target, CREATE, f"will be copied from env {secret['env']}",
                                       _block=block, _secret=secret))
                else:
                    items.append(_item("secret", target, SECRET,
                                       f"absent; load {secret['vault']} from the vault and set env {secret['env']} "
                                       "(or put it in HKCU yourself)"))
        return items

    def _config_value(self, key: str, name: str, wanted: str, target: str) -> dict[str, Any]:
        current = self.host.registry_get(key, name)
        if current is None:
            if self.apply:
                self.host.registry_set(key, name, wanted)
            return _item("config", target, CREATE, "absent")
        if current == wanted:
            return _item("config", target, UNCHANGED, "matches")
        return _item("config", target, KEPT, f"live={current} manifest={wanted} (operator-owned, kept)")

    def apply_registry(self, secrets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        items = []
        for secret_item in secrets:
            if secret_item["status"] == CREATE and self.apply:
                self.host.registry_set(secret_item["_block"]["key"], secret_item["_secret"]["name"],
                                       self.env[secret_item["_secret"]["env"]])
            items.append({k: v for k, v in secret_item.items() if not k.startswith("_")})
        live_names = {t["name"] for t in self.live_tasks}
        for block in self.manifest["registry"]:
            if block["task"] not in live_names:
                continue
            for name, template in block["values"].items():
                items.append(self._config_value(block["key"], name, self.fill(template), f"HKCU\\{block['key']}\\{name}"))
        if not self.only:  # the user environment belongs to no single task
            for name, template in self.manifest["user_env"].items():
                items.append(self._config_value("Environment", name, self.fill(template), f"HKCU\\Environment\\{name}"))
        return items

    # ---- tasks
    def task_items(self) -> list[dict[str, Any]]:
        items = []
        for task in self.live_tasks:
            for external in task.get("external", []) + task.get("optional_external", []):
                path = self.fill(external["path"])
                optional = external in task.get("optional_external", [])
                if Path(path).exists():
                    items.append(_item("external", path, UNCHANGED, f"present ({external['owner']})"))
                else:
                    items.append(_item("external", path, OPTIONAL if optional else EXTERNAL,
                                       f"provided by the {external['owner']}; restore it there"))
            xml = task_xml(self.manifest, task, self.values, self.declared)
            wanted = normalize_xml(xml)
            live_xml = self.host.query_task_xml(task["name"])
            if live_xml is None:
                status, detail = CREATE, "task absent"
            else:
                differences = diff_normalized(normalize_xml(live_xml), wanted)
                if not differences:
                    items.append(_item("task", task["name"], UNCHANGED, "matches the manifest"))
                    continue
                status, detail = UPDATE, "; ".join(differences)
            if self.apply:
                if live_xml is not None:
                    self.backup_dir.mkdir(parents=True, exist_ok=True)
                    (self.backup_dir / f"{task['name']}.{self.stamp}.xml").write_text(live_xml, encoding="utf-8")
                self.host.register_task(task["name"], xml)
            items.append(_item("task", task["name"], status, detail))
        return items


# --------------------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memorymaster-setup windows-tasks",
        description="Plan, apply or check the MemoryMaster scheduled tasks from ops/windows-tasks/manifest.json.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="print what --apply would do (default)")
    mode.add_argument("--apply", action="store_true", help="create runtimes, wrappers, config and tasks")
    mode.add_argument("--check", action="store_true", help="diff live state against the manifest; exit 1 on drift")
    parser.add_argument("--repo", help="memorymaster checkout holding the manifest (default: this checkout)")
    parser.add_argument("--var", action="append", default=[], metavar="NAME=VALUE", help="set a manifest variable")
    parser.add_argument("--only", action="append", default=[], metavar="TASK", help="limit to a task (repeatable)")
    parser.add_argument("--wheel", help="memorymaster wheel to install (default: build it from --repo)")
    parser.add_argument("--wheel-sha256", default="", help="refuse a wheel whose SHA-256 differs")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--show-paths", action="store_true", help="do not replace machine paths by {VAR} tokens")
    return parser


def main(argv: list[str] | None = None, *, host: Any = None, env: dict[str, str] | None = None,
         out: Callable[[str], None] = print) -> int:
    args = build_parser().parse_args(argv)
    env = dict(os.environ) if env is None else env
    try:
        cli_vars = {}
        for pair in args.var:
            name, sep, value = pair.partition("=")
            if not sep or not name:
                raise WindowsTasksError(f"--var expects NAME=VALUE, got {pair!r}")
            cli_vars[name] = value
        repo = find_repo(args.repo)
        manifest = load_manifest(repo)
        values, declared = build_vars(manifest, repo, env, cli_vars)
        mode = "apply" if args.apply else "check" if args.check else "plan"
        if host is None:
            if sys.platform == "win32":
                host = WindowsHost()
            elif mode == "plan":
                host = PlanOnlyHost()
            else:
                raise WindowsTasksError(f"--{mode} needs Windows (schtasks and the registry)")
        reconciler = Reconciler(
            repo, manifest, values, declared, host, env, args.only, apply=args.apply,
            wheel=Path(args.wheel) if args.wheel else None, wheel_sha256=args.wheel_sha256,
        )
        items = reconciler.run()
    except WindowsTasksError as exc:
        print(f"windows-tasks: {exc}", file=sys.stderr)
        return 2
    redact = make_redactor(values, not args.show_paths)
    drift = [i for i in items if i["status"] not in _NOT_DRIFT]
    if args.json:
        out(json.dumps({"mode": mode, "items": [{**i, "detail": redact(i["detail"]), "target": redact(i["target"])}
                                                for i in items], "drift": len(drift)}, indent=2))
    else:
        verb = {"plan": "would be", "apply": "done", "check": "live"}[mode]
        for item in items:
            out(f"[{item['status']:<15}] {item['kind']:<8} {redact(item['target'])}  {redact(item['detail'])}")
        out(f"{len(items)} items ({verb}); {len(drift)} not matching the manifest.")
    return 1 if mode == "check" and drift else 0


if __name__ == "__main__":
    raise SystemExit(main())

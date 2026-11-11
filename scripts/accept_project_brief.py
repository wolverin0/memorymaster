#!/usr/bin/env python3
"""Acceptance of the project brief (T-2278): what each CLI adapter WOULD emit at SessionStart, per project.

For every configured project (the ``MEMORYMASTER_PROJECT_PROJECTION_*`` environment, as the hooks read it) and every
CLI, render the SessionStart context through that adapter's own code path and check that it names a card, the
next roadmap item (or ``none``) and at least one failure with its source tag.  Read-only: it only reads the
published projection; hook state, hook events and the session delta are redirected to a throwaway directory.

    python scripts/accept_project_brief.py [--projects memorymaster,infra] [--clis claude,codex] [--home DIR]
        [--min-pass N] [--json]

``--home`` points HOME at a fixture directory (tests); without it the live environment is read.
The exit code is 0 when at least ``--min-pass`` (default: all) of the project x CLI cases pass.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import runpy
import sys
import tempfile
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

CLIS = ("claude", "codex", "agy", "opencode")
CODEX_TEMPLATE = REPO_ROOT / "memorymaster" / "config_templates" / "hooks" / "memorymaster-codex-recall.py"
_TITLES = ("RUNNING", "NEXT", "DECISIONS", "FAILURES")
_CARD = re.compile(r"^- [A-Za-z]+-\d+ \w+:")
_ROADMAP = re.compile(r"^- .*\[ROADMAP\.md(#L\d+)?\]$")
_SOURCED = re.compile(r"\[[^\]\s]+\]$")


def sections(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        line = line.strip()
        if line in _TITLES:
            current = out.setdefault(line, [])
        elif current is not None and line.startswith("- "):
            current.append(line)
    return out


def check(text: str) -> dict[str, bool]:
    """The three acceptance facts of a SessionStart context."""
    found = sections(text)
    failures = [ln for ln in found.get("FAILURES", []) if not ln.startswith("- none")]
    return {
        "card": any(_CARD.match(ln) for ln in found.get("RUNNING", [])),
        "roadmap": any(_ROADMAP.match(ln) for ln in found.get("NEXT", [])),
        "failure": any(_SOURCED.search(ln) for ln in failures),
    }


def render(cli: str, cwd: str, db_path: str) -> str:
    """The context ``cli``'s adapter emits at SessionStart in ``cwd`` ("" when it emits nothing)."""
    sid = f"accept-{uuid.uuid4().hex[:12]}"
    data = {"cwd": cwd, "session_id": sid}
    from memorymaster.profile.project_runtime import session_start_context

    if cli == "claude":  # memorymaster-session-start.py writes exactly this string
        return session_start_context(data) or ""
    if cli == "codex":  # the same string through the adapter's 1,800 B cut
        return runpy.run_path(str(CODEX_TEMPLATE))["fit_context"](session_start_context(data) or "")
    if cli == "agy":
        from memorymaster.surfaces.agy_hooks import run_hook

        out = run_hook("SessionStart", {"conversationId": sid, "workspacePaths": [cwd]}, db_path=db_path)
        return json.loads(out)["injectSteps"][0]["ephemeralMessage"] if out else ""
    if cli == "opencode":
        from memorymaster.recall.delivery import recall_block
        from memorymaster.surfaces.hook_recall import session_brief

        brief = session_brief(cwd, f"opencode:{sid}")
        return recall_block(brief) if brief else ""
    raise ValueError(f"unknown cli {cli!r}")


def accept(configs, clis=CLIS) -> list[dict]:
    """One result row per project x CLI; never writes outside a temporary state directory."""
    rows = []
    with tempfile.TemporaryDirectory(prefix="accept-brief-") as scratch:
        os.environ.update({"MEMORYMASTER_RECALL_STATE_DIR": scratch, "MEMORYMASTER_HOOK_EVENTS_LOG": f"{scratch}/events",
                           "MEMORYMASTER_PROJECT_SESSION_DELTA": "0", "MEMORYMASTER_HOOK_RECALL_REMOTE": "0"})
        for config in configs:
            for cli in clis:
                try:
                    text = render(cli, str(config.project_root), str(config.db_path))
                except Exception as exc:  # noqa: BLE001 - one broken adapter is a failed case, not a crashed run
                    text, error = "", type(exc).__name__
                else:
                    error = None
                facts = check(text)
                rows.append({"project": config.slug, "cli": cli, "bytes": len(text.encode("utf-8")), **facts,
                             "pass": all(facts.values()), **({"error": error} if error else {})})
    return rows


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--projects", default="", help="comma-separated slugs (default: every configured project)")
    parser.add_argument("--clis", default=",".join(CLIS))
    parser.add_argument("--home", type=Path, default=None, help="fixture HOME (tests); default: the live environment")
    parser.add_argument("--min-pass", type=int, default=None, help="passing cases required (default: all)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.home:
        os.environ["HOME"] = os.environ["USERPROFILE"] = str(args.home)
    from memorymaster.profile.project_runtime import load_project_configs

    wanted = {s.strip() for s in args.projects.split(",") if s.strip()}
    configs = [c for c in load_project_configs() if not wanted or c.slug in wanted]
    clis = [c.strip() for c in args.clis.split(",") if c.strip()]
    rows = accept(configs, clis)
    passed = sum(row["pass"] for row in rows)
    if args.json:
        print(json.dumps({"passed": passed, "total": len(rows), "rows": rows}, indent=2))
    else:
        for row in rows:
            marks = " ".join(f"{k}={'ok' if row[k] else 'MISSING'}" for k in ("card", "roadmap", "failure"))
            print(f"{'PASS' if row['pass'] else 'FAIL'} {row['project']:<14} {row['cli']:<9} {row['bytes']:>5} B  {marks}"
                  + (f"  error={row['error']}" if row.get("error") else ""))
        print(f"passed {passed}/{len(rows)}")
    return 0 if rows and passed >= (len(rows) if args.min_pass is None else args.min_pass) else 1


if __name__ == "__main__":
    raise SystemExit(main())

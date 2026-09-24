"""Fail when installed selection code or client launchers still use old sources.

Run with the target Python and -I; this reads package/configuration files only.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import tomllib
import zipfile


def check_package(wheel: Path) -> dict:
    import memorymaster
    from memorymaster.dreaming.source_review import REVIEW_VERSION

    root = Path(memorymaster.__file__).resolve().parent.parent
    with zipfile.ZipFile(wheel) as archive:
        names = [name for name in archive.namelist()
                 if name.startswith("memorymaster/") and not name.endswith("/")]
        mismatches = [name for name in names if not (root / name).is_file()
                      or (root / name).read_bytes() != archive.read(name)]
    return {"installed_origin": root.name == "site-packages", "selection_version": REVIEW_VERSION,
            "files_checked": len(names), "mismatches": mismatches,
            "ok": root.name == "site-packages" and REVIEW_VERSION == 2 and not mismatches}


def check_clients(home: Path) -> dict:
    codex = tomllib.loads((home / ".codex/config.toml").read_text(encoding="utf-8"))
    claude = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
    launches = {"codex": codex["mcp_servers"]["memorymaster"]["args"],
                "claude": claude["mcpServers"]["memorymaster"]["args"]}
    expected = ["-I", "-m", "memorymaster.mcp_server"]
    launch_ok = {name: args == expected for name, args in launches.items()}
    names = ("auto-ingest", "dream-sync", "recall", "session-start", "steward-cycle")
    hooks = {}
    for name in names:
        text = (home / ".claude/hooks" / f"memorymaster-{name}.py").read_text(encoding="utf-8")
        hooks[name] = "sys.path.insert(0," not in text and "sys.path.append(" in text
    return {"launchers": launch_ok, "hooks": hooks,
            "ok": all(launch_ok.values()) and all(hooks.values())}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--clients-home", type=Path)
    args = parser.parse_args()
    result = {"package": check_package(args.wheel)}
    if args.clients_home:
        result["clients"] = check_clients(args.clients_home)
    print(json.dumps(result, sort_keys=True))
    return 0 if all(value["ok"] for value in result.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Read-only preflight for the current checkout's derived GitNexus index."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess

CODE_SUFFIXES = {".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".cpp", ".h", ".cs", ".java", ".sql"}
EXCLUDED = ("repos/", "repo/", "cloned/", "artifacts/", "graphify-out/", "delta-exchange/", ".venv/")


def inspect_index(root: Path) -> dict:
    """Never equate matching commit IDs with a fully indexed working tree."""
    root = root.resolve()
    head = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    raw = subprocess.check_output(
        ["git", "-C", str(root), "status", "--porcelain=v1", "-z", "--untracked-files=all",
         "--", ".", *(f":(exclude){prefix}" for prefix in EXCLUDED)],
    ).decode("utf-8", errors="replace")
    changed = []
    entries = iter(raw.split("\0"))
    for entry in entries:
        if not entry:
            continue
        status, name = entry[:2], entry[3:].replace("\\", "/")
        names = [name]
        if "R" in status or "C" in status:
            names.append(next(entries, ""))
        for name in names:
            if not name.startswith(EXCLUDED) and Path(name).suffix.lower() in CODE_SUFFIXES:
                changed.append({"status": status, "path": name})
    result = {"head": head, "index_present": False, "issues": [], "changed_source": changed}
    meta_path = root / ".gitnexus" / "meta.json"
    if not meta_path.exists():
        result["issues"].append("INDEX_MISSING")
    else:
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8-sig"))
            result.update(index_present=True, indexed_commit=meta.get("lastCommit"), indexed_at=meta.get("indexedAt"), embeddings=meta.get("stats", {}).get("embeddings", 0))
            recorded = meta.get("repoPath")
            if not recorded or os.path.normcase(str(Path(recorded).resolve())) != os.path.normcase(str(root)):
                result["issues"].append("WRONG_CHECKOUT")
            if meta.get("lastCommit") != head:
                result["issues"].append("STALE_HEAD")
        except (ValueError, TypeError, AttributeError):
            result["issues"].append("INVALID_METADATA")
    if changed:
        result["issues"].append("SOURCE_DIRTY_VERIFY_LIVE")
    result["status"] = "PASS" if not result["issues"] else "WARN"
    result["instruction"] = "Use live source for changed/new files; preserve embeddings when reindexing. Never switch to a sibling worktree index implicitly."
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    result = inspect_index(args.root)
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

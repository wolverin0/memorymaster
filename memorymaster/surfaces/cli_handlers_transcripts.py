"""CLI for the opt-in raw-transcript fallback (T-1860).

``transcript-index`` builds/refreshes the sidecar index from local Claude Code
transcripts; ``transcript-search`` returns dated, cited fragments from it.
Neither command opens the claims DB or creates claims, so both run without a
``MemoryService``.  Use them when ``query`` / ``recall`` return nothing useful.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

TRANSCRIPT_COMMANDS = ("transcript-index", "transcript-search")


def register_transcript_parsers(sub: Any) -> None:
    index = sub.add_parser(
        "transcript-index",
        help="Index local Claude Code transcripts into a sidecar FTS5 file (opt-in raw fallback; no claims)",
    )
    index.add_argument("--root", default=None, help="Transcript root (default: ~/.claude/projects)")
    index.add_argument("--index", dest="index_path", default=None,
                       help="Sidecar index file (default: ~/.memorymaster/transcripts.db)")
    index.add_argument("--since-days", type=float, default=None,
                       help="Only scan files modified in the last N days (cap for a first run)")
    index.add_argument("--include-automated", action="store_true",
                       help="Also index headless `claude -p` / SDK sessions (skipped by default: they quote prompts and pollute results)")
    index.add_argument("--max-files", type=int, default=None, help="Stop after indexing N files")

    search = sub.add_parser(
        "transcript-search",
        help="Search the raw-transcript index: dated, cited fragments (relevance first, newest as tiebreak)",
    )
    search.add_argument("query", help="Natural-language question or keywords")
    search.add_argument("--index", dest="index_path", default=None, help="Sidecar index file")
    search.add_argument("--limit", type=int, default=5, help="Max fragments (default 5)")
    search.add_argument("--scope", default=None, help="Only this project scope, e.g. project:memorymaster")
    search.add_argument("--since", default=None, help="Only fragments on/after this ISO date")


def _index(args: Any, service: Any, parser: Any, effective_db: str) -> int:
    from memorymaster.recall import transcript_index as ti

    stats = ti.index_transcripts(
        Path(args.root) if args.root else None,
        Path(args.index_path) if args.index_path else None,
        since_days=args.since_days,
        max_files=args.max_files,
        include_automated=args.include_automated,
    )
    if args.json_output:
        print(json.dumps({"ok": True, **stats}))
    else:
        print("transcript-index: " + ", ".join(f"{k}={v}" for k, v in stats.items()))
    return 0


def _search(args: Any, service: Any, parser: Any, effective_db: str) -> int:
    from memorymaster.recall import transcript_index as ti

    hits = ti.search(
        args.query,
        Path(args.index_path) if args.index_path else None,
        limit=max(1, min(args.limit, 50)),
        scope=args.scope,
        since=args.since,
    )
    if args.json_output:
        print(json.dumps({"ok": True, "rows": len(hits), "hits": [h.to_dict() for h in hits]}))
        return 0
    if not hits:
        if ti.index_status(Path(args.index_path) if args.index_path else None) == "outdated":
            print("transcript-search: the index was built by an older version and is not served; "
                  "rebuild it with `memorymaster transcript-index`")
        else:
            print("transcript-search: no fragments (index missing or no match; run transcript-index first)")
        return 0
    # Fragments are arbitrary UTF-8; a cp1252 Windows console must degrade, not crash.
    sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
    for h in hits:
        print(f"[{h.date}] session={h.session_id} {h.scope} lines {h.first_line}-{h.last_line} score={h.score}")
        print("  " + " ".join(h.excerpt.split()))
    return 0


TRANSCRIPT_COMMAND_HANDLERS = {"transcript-index": _index, "transcript-search": _search}

__all__ = ["TRANSCRIPT_COMMANDS", "TRANSCRIPT_COMMAND_HANDLERS", "register_transcript_parsers"]

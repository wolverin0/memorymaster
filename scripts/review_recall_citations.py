#!/usr/bin/env python3
"""Read-only review: how many prompt-recall injections does the model actually cite?

Walks Claude Code transcripts, pairs every UserPromptSubmit "[MemoryMaster recall]"
injection with the ``mm-<id>`` tokens the assistant wrote in the same turn (the
turn ends at the next real user prompt or a compaction boundary) and reports
counts per lane. It never opens the claims database and emits only counts, claim
short ids and lane names.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

MARKER = "[MemoryMaster recall]"
ID = r"mm-[0-9a-f]+(?:~\d+)?"
INJECTED_RE = re.compile(rf"^\s*-\s*\[({ID})\]", re.MULTILINE)
BULLET_RE = re.compile(r"^- ", re.MULTILINE)
BRACKET_RE = re.compile(rf"\[\s*({ID}(?:\s*[,;]\s*{ID})*)\s*\]")
BARE_RE = re.compile(rf"(?<![\w~-])({ID})(?![\w~])")
TOP_N = 20
PROJECT_ANCHOR = "Py-Apps-"
NOTIFICATION = "<task-notification>"
CITE_HINTS = ("cite its [mm-id]", "cite [mm-id] when you rely")  # current / header format (T-1632)
HEADER_FORMAT_HINT = "Decisions you made earlier"


def parse_time(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def load_lane_map(path):
    lanes = json.loads(Path(path).read_text(encoding="utf-8")).get("lanes", [])
    repos = {}
    for lane in lanes:
        for repo in lane.get("repos", []):
            repos[repo.lower()] = lane["lane"]
    return [lane["lane"] for lane in lanes], repos


def slug_to_lane(slug, repos):
    """Longest repo name that equals the slug or prefixes it at a dash boundary."""
    slug = slug.lower().replace("_", "-").replace(".", "-")
    best = None
    for repo, lane in repos.items():
        key = repo.replace("_", "-").replace(".", "-").replace(" ", "-")
        if slug == key or slug.startswith(key + "-"):
            if best is None or len(key) > len(best[0]):
                best = (key, lane)
    return best[1] if best else None


def other_lane(slug, home_name):
    """other:<slug>, with anything derived from the OS username scrubbed."""
    slug = slug.lower()
    if home_name and home_name.lower() in slug.replace("_", "-").split("-"):
        return "other:user-home"
    return f"other:{slug}"


def lane_for_project_dir(name, repos, home_name=""):
    """Transcript dir name -> lane. Never returns the raw dir (it carries the username)."""
    index = name.find(PROJECT_ANCHOR)
    if index < 0:
        return "other:external"
    rest = name[index + len(PROJECT_ANCHOR):]
    if rest.startswith("-"):  # hidden dir at the root, e.g. a ".worktrees/<repo>-..." checkout
        rest = rest.lstrip("-")
        for hidden in ("worktrees-", "wt-"):
            if rest.startswith(hidden):
                rest = rest[len(hidden):]
    slug = rest.split("--")[0]
    if not slug:
        return "other:py-apps-root"
    return slug_to_lane(slug, repos) or other_lane(slug, home_name)


def lane_for_scope(scope, repos, home_name=""):
    if isinstance(scope, str) and scope.startswith("project:"):
        slug = scope.split(":", 1)[1]
        return slug_to_lane(slug, repos) or other_lane(slug, home_name)
    return other_lane(scope, home_name) if isinstance(scope, str) else "other:unknown"


def is_real_prompt(row):
    if row.get("type") != "user" or row.get("isMeta") or row.get("isCompactSummary"):
        return False
    content = (row.get("message") or {}).get("content")
    if isinstance(content, str):
        return True
    return isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") in ("text", "image") for block in content
    )


def prompt_text(row):
    content = (row.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content
                     if isinstance(b, dict) and b.get("type") == "text")


def assistant_text(row):
    content = (row.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        block.get("text", "") for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


PROMPT_KEEP = 4000
PERSISTED = "<persisted-output>"
PERSISTED_PATH_RE = re.compile(r"Full output saved to:\s*(.+)")


def read_persisted(item, root, diagnostics):
    """Full recall payload behind a "<persisted-output>" stub (read-only), else the preview."""
    preview = item.find(MARKER)
    if preview < 0:
        return None
    found = PERSISTED_PATH_RE.search(item)
    if found and root is not None:
        try:
            target = Path(found.group(1).strip()).resolve()
            if target.is_file() and target.is_relative_to(root.resolve()):
                diagnostics["persisted_recall_full"] += 1
                return target.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass
    diagnostics["persisted_recall_preview_only"] += 1
    return item[preview:]


def recall_texts(row, root=None, diagnostics=None):
    """Recall payloads of a UserPromptSubmit hook attachment, else []."""
    attachment = row.get("attachment")
    if row.get("type") != "attachment" or not isinstance(attachment, dict):
        return []
    if attachment.get("type") != "hook_additional_context":
        return []
    if attachment.get("hookEvent") != "UserPromptSubmit":
        return []
    content = attachment.get("content")
    items = [content] if isinstance(content, str) else content if isinstance(content, list) else []
    texts = []
    for item in items:
        if not isinstance(item, str):
            continue
        head = item.lstrip()
        if head.startswith(MARKER):
            texts.append(item)
        elif head.startswith(PERSISTED):
            full = read_persisted(item, root, diagnostics if diagnostics is not None else Counter())
            if full is not None and full.lstrip().startswith(MARKER):
                texts.append(full)
    return texts


class Turn:
    __slots__ = ("key", "ts", "real", "instructed", "ids", "cited", "lenient", "prompt", "blocks", "header_format")

    def __init__(self, key, ts, real):
        self.key, self.ts, self.real = key, ts, real
        self.instructed = False
        self.ids, self.cited, self.lenient = set(), set(), set()
        self.prompt, self.blocks, self.header_format = "", [], False  # kept in memory for the offline replay only


def scan_claude_file(path, diagnostics, turns, in_window, root=None, soft_notifications=False):
    """Append every turn of the file to turns[side]; side True is inline sidechain rows."""
    state = {False: None, True: None}
    with path.open(encoding="utf-8", errors="replace") as stream:
        for number, line in enumerate(stream):
            has_hook = "hook_additional_context" in line
            if has_hook and MARKER not in line:
                continue
            if not has_hook and not any(t in line for t in ('"user"', '"assistant"', "compact_boundary")):
                continue
            try:
                row = json.loads(line)
            except ValueError:
                diagnostics["malformed_transcript_lines"] += 1
                continue
            if not isinstance(row, dict):
                continue
            side = bool(row.get("isSidechain"))
            kind = row.get("type")
            ts = parse_time(row.get("timestamp"))
            counted = ts is not None and in_window(ts)  # diagnostics cover the window only
            if kind == "system" and row.get("subtype") == "compact_boundary":
                diagnostics["compaction_boundaries"] += counted
                state[side] = None
            elif kind == "user" and row.get("isCompactSummary"):
                diagnostics["compaction_boundaries"] += counted
                state[False] = state[True] = None
            elif is_real_prompt(row):
                if (soft_notifications and state[side] is not None
                        and prompt_text(row).lstrip().startswith(NOTIFICATION)):
                    continue  # sensitivity variant: background-task pings do not end the turn
                key = row.get("uuid") or f"{path.name}:{number}"
                state[side] = Turn(key, ts, real=True)
                state[side].prompt = prompt_text(row)[:PROMPT_KEEP]
                turns[side].append(state[side])
            elif kind == "attachment":
                texts = recall_texts(row, root, diagnostics if counted else None)
                if not texts:
                    continue
                turn = state[side]
                if turn is None:  # injection with no prompt row before it
                    turn = state[side] = Turn(row.get("uuid") or f"{path.name}:{number}", ts, real=False)
                    turns[side].append(turn)
                if turn.ts is None:
                    turn.ts = ts
                for text in texts:
                    found = INJECTED_RE.findall(text)
                    if not found and counted:
                        diagnostics["injections_without_ids"] += 1
                        diagnostics["claims_in_injections_without_ids"] += len(BULLET_RE.findall(text))
                    turn.ids.update(found)
                    turn.instructed |= any(hint in text for hint in CITE_HINTS)
                    turn.header_format |= HEADER_FORMAT_HINT in text
                    turn.blocks.append(text)
            elif kind == "assistant" and state[side] is not None and "mm-" in line:
                text = assistant_text(row)
                for group in BRACKET_RE.findall(text):
                    state[side].cited.update(re.findall(ID, group))
                state[side].lenient.update(BARE_RE.findall(text))


def empty_stats():
    return {"prompts_total": 0, "prompts_with_injection": 0, "injected_claims": 0,
            "cited_claims": 0, "cited_claims_lenient": 0, "prompts_with_citation": 0,
            "prompts_with_citation_lenient": 0, "prompts_header_format": 0}


def finalize(stats, injected, cited):
    stats["cited_injected_rate"] = (stats["cited_claims"] / stats["injected_claims"]
                                    if stats["injected_claims"] else None)
    stats["cited_injected_rate_lenient"] = (stats["cited_claims_lenient"] / stats["injected_claims"]
                                            if stats["injected_claims"] else None)
    stats["unique_injected_ids"] = len(injected)
    never = [(i, n) for i, n in injected.items() if not cited.get(i)]
    never.sort(key=lambda item: (-item[1], item[0]))
    stats["most_injected_never_cited"] = [{"id": i, "injected": n} for i, n in never[:TOP_N]]
    top = sorted(cited.items(), key=lambda item: (-item[1], item[0]))
    stats["top_cited"] = [{"id": i, "cited": n} for i, n in top[:TOP_N]]
    return stats


def aggregate(turns):
    """Finalized stats for an iterable of already-deduped turns."""
    stats, injected, cited = empty_stats(), Counter(), Counter()
    for turn in turns:
        stats["prompts_total"] += turn.real
        if not turn.ids:
            continue
        hit, hit_lenient = turn.ids & turn.cited, turn.ids & turn.lenient
        stats["prompts_with_injection"] += 1
        stats["prompts_header_format"] += turn.header_format
        stats["injected_claims"] += len(turn.ids)
        stats["cited_claims"] += len(hit)
        stats["cited_claims_lenient"] += len(hit_lenient)
        stats["prompts_with_citation"] += bool(hit)
        stats["prompts_with_citation_lenient"] += bool(hit_lenient)
        injected.update(turn.ids)
        cited.update(hit)
    return finalize(stats, injected, cited)


def merge_turns(turns):
    """Dedupe turns by key: a prompt seen in a resumed copy of the session keeps the union of both."""
    merged = {}
    for turn in turns:
        known = merged.setdefault(turn.key, turn)
        if known is not turn:
            known.ids |= turn.ids
            known.cited |= turn.cited
            known.lenient |= turn.lenient
            known.real |= turn.real
            known.instructed |= turn.instructed
            known.header_format |= turn.header_format
            known.prompt = known.prompt or turn.prompt
            known.blocks.extend(b for b in turn.blocks if b not in known.blocks)
    return list(merged.values())


def summarize(lane_turns):
    """lane -> list[Turn]; returns {lane: stats}, total stats, total split by cite instruction."""
    lanes, everything = {}, []
    for lane in sorted(lane_turns):
        merged = merge_turns(lane_turns[lane])
        lanes[lane] = aggregate(merged)
        everything.extend(merged)
    by_instruction = {
        "instructed": aggregate(t for t in everything if t.ids and t.instructed),
        "uninstructed": aggregate(t for t in everything if t.ids and not t.instructed),
    }
    return lanes, aggregate(everything), by_instruction


def scan_dense(path, cutoff, repos, diagnostics, home_name=""):
    lanes = {}
    if not path.exists():
        return {"lanes": lanes, "total": {"events": 0, "injected_claims": 0}}
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except ValueError:
                diagnostics["malformed_dense_lines"] += 1
                continue
            ts = parse_time(row.get("ts")) if isinstance(row, dict) else None
            if ts is None or ts < cutoff:
                continue
            lane = lane_for_scope(row.get("scope"), repos, home_name)
            entry = lanes.setdefault(lane, {"events": 0, "injecting_events": 0, "injected_claims": 0,
                                            "outcomes": {}})
            ids = row.get("injected_ids") or []
            entry["events"] += 1
            entry["injecting_events"] += bool(ids)
            entry["injected_claims"] += len(ids)
            outcome = str(row.get("outcome", "unknown")).split(":")[0]
            entry["outcomes"][outcome] = entry["outcomes"].get(outcome, 0) + 1
    total = {"events": sum(v["events"] for v in lanes.values()),
             "injected_claims": sum(v["injected_claims"] for v in lanes.values())}
    return {"lanes": dict(sorted(lanes.items())), "total": total}


def probe_codex(sessions, cutoff, diagnostics):
    """Presence probe only: does Codex receive the injection as a user-role message?"""
    scanned = events = 0
    if sessions.exists():
        for path in sessions.rglob("*.jsonl"):
            try:
                if datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) < cutoff:
                    continue
            except OSError:
                continue
            scanned += 1
            with path.open(encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    if MARKER not in line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    payload = row.get("payload") if isinstance(row, dict) else None
                    if not (isinstance(payload, dict) and row.get("type") == "response_item"
                            and payload.get("type") == "message" and payload.get("role") == "user"):
                        continue
                    for block in payload.get("content") or []:
                        text = block.get("text", "") if isinstance(block, dict) else ""
                        events += text.lstrip().startswith(MARKER)
    return {"files_scanned": scanned, "injection_events": events,
            "injection_format_observed": events > 0}


def collect_transcripts(home, repos, cutoff, now, diagnostics, soft_notifications=False, raw=None):
    """Scan Claude transcripts; returns summarize() of main and subagent turns, then file counts.

    ``raw``, when a dict, receives the main-transcript turns as ``{lane: [Turn]}`` (offline replay).
    """
    def in_window(ts):
        return cutoff <= ts <= now + timedelta(days=1)

    projects = home / ".claude" / "projects"
    turns = {False: {}, True: {}}
    files = {"main": 0, "subagent": 0}
    if projects.exists():
        found = [(p, False) for p in projects.glob("*/*.jsonl")]
        found += [(p, True) for p in projects.glob("*/*/subagents/*.jsonl")]
        for path, is_sub in sorted(found):
            try:
                if datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) < cutoff:
                    continue
            except OSError:
                continue
            lane = lane_for_project_dir(path.relative_to(projects).parts[0], repos, home.name)
            files["subagent" if is_sub else "main"] += 1
            file_turns = {False: [], True: []}
            scan_claude_file(path, diagnostics, file_turns, in_window, projects, soft_notifications)
            for side in (False, True):
                bucket = is_sub or side  # inline sidechain rows are subagent activity
                kept = [t for t in file_turns[side] if t.ts is not None and in_window(t.ts)]
                turns[bucket].setdefault(lane, []).extend(kept)

    if raw is not None:
        raw.update({lane: merge_turns(found) for lane, found in turns[False].items()})
    return (*summarize(turns[False]), *summarize(turns[True]), files)


def review(home, mapping_path, days=7, now=None, sensitivity=False):
    home = Path(home)
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    lane_names, repos = load_lane_map(mapping_path)
    diagnostics = Counter({"malformed_transcript_lines": 0, "malformed_dense_lines": 0,
                           "compaction_boundaries": 0, "injections_without_ids": 0,
                           "claims_in_injections_without_ids": 0,
                           "persisted_recall_full": 0, "persisted_recall_preview_only": 0})
    main_lanes, main_total, main_split, sub_lanes, sub_total, _, files = collect_transcripts(
        home, repos, cutoff, now, diagnostics)
    silent = sorted(name for name in lane_names
                    if main_lanes.get(name, {}).get("prompts_with_injection", 0) == 0)
    other_silent = sorted(name for name, s in main_lanes.items()
                          if name.startswith("other:") and s["prompts_with_injection"] == 0
                          and s["prompts_total"] > 0)
    result = {
        "generated_at": now.isoformat(),
        "window_days": days,
        "window_start": cutoff.isoformat(),
        "files_scanned": files,
        "lanes": main_lanes,
        "total": main_total,
        "total_by_cite_instruction": main_split,
        "subagents": {"lanes": sub_lanes, "total": sub_total},
        "lanes_without_observed_injection": silent,
        "lanes_without_observed_injection_prompts_total": {
            name: main_lanes.get(name, {}).get("prompts_total", 0) for name in silent},
        "unmapped_lanes_with_prompts_but_no_injection": other_silent,
        "dense_cross_check": scan_dense(home / ".memorymaster" / "metrics" / "recall-dense.jsonl",
                                        cutoff, repos, diagnostics, home.name),
        "providers": {"codex": probe_codex(home / ".codex" / "sessions", cutoff, diagnostics)},
        "diagnostics": dict(diagnostics),
    }
    if sensitivity:
        soft = collect_transcripts(home, repos, cutoff, now, Counter(), soft_notifications=True)
        result["sensitivity_task_notification_continues_turn"] = {
            "lanes": soft[0], "total": soft[1], "total_by_cite_instruction": soft[2]}
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--out", help="write the JSON report here (default: stdout)")
    parser.add_argument("--home", default=str(Path.home()), help=argparse.SUPPRESS)
    parser.add_argument("--lanes", help="orchestrators.json (default: Py Apps/_intel/orchestrators.json)")
    args = parser.parse_args(argv)
    mapping = args.lanes
    if not mapping:
        for parent in Path(__file__).resolve().parents:
            candidate = parent / "_intel" / "orchestrators.json"
            if candidate.exists():
                mapping = candidate
                break
    if not mapping:
        parser.error("orchestrators.json not found; pass --lanes")
    text = json.dumps(review(args.home, mapping, days=args.days, sensitivity=True), indent=2, sort_keys=True)
    if args.out:
        out = Path(args.out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
    else:
        sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

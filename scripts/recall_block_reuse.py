#!/usr/bin/env python3
"""Read-only reuse metric for the recall hook's injected block (T-2548).

Question: when the UserPromptSubmit hook injects a ``[MemoryMaster recall]`` / ``[MemoryMaster residual]`` block,
does the assistant's answer to that prompt use words that came from the block?

Per block (main thread only): distinctive words = block words MINUS the prompt's words MINUS the block's own
framing (headers, labels, ids, dates). used = distinct block words present in the answer (all assistant TEXT of the
turn: no tool input, tool result or thinking). Reported as any-use (>= 1 word) and strong-use (>= ``--min-words``),
plus whether the answer cites an injected ``[mm-id]``.

Controls (the number means nothing without them):
  1. prompt words are removed before counting;
  2. chance: every block is also scored against the answers of other turns (same session when it has one, else
     the same project; seeded, ``--controls`` partners per block). The headline is observed rate vs shuffled rate.

Every figure is split by part (claims / excerpts / residual / all) and by project (last cwd segment). Output is
counts, rates and mm-ids only: no prompt, answer or block text, no paths, session ids are never printed.
Transcripts are opened for reading only; nothing is written except stdout (and ``--out``).

It cannot tell: a block that informed an answer without sharing a word; overlap that is coincidence the shuffle
under- or over-estimates (same-session partners share vocabulary, so the shuffle is conservative); anything
outside Claude Code transcripts.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from memorymaster.recall import recall_format, residual, transcript_fallback  # noqa: E402
from memorymaster.recall.context_hook import RECALL_HEADER  # noqa: E402
from memorymaster.recall.delivery import RECALL_PREFIX  # noqa: E402

RECALL_MARK = RECALL_PREFIX.strip()
RESIDUAL_MARK = residual.HEADER.split("]")[0] + "]"
PARTS = ("claims", "excerpts", "residual", "all")
MAX_LINE = 8_000_000  # a longer JSONL line is skipped unread (shortcut: no tool/prompt this big matters here)
PROMPT_CAP = 20_000
ANSWER_CAP = 20_000
POOL_CAP = 2_000  # answers kept per file as shuffle partners (reservoir)
PROJECT_POOL_CAP = 300  # answers kept per project for sessions with no other answered turn

_KIND = re.compile(r'"type":\s*"(user|assistant|attachment)"')
_MM_ID = re.compile(r"mm-[0-9a-zA-Z]+")
_BULLET_ID = re.compile(r"^- \[(mm-[0-9a-zA-Z]+)(?:~\d+)?\]\s*")
_LABEL = re.compile(r"^- \[[^\]]*\]\s*")  # an excerpt line starts with its "[date project session=... lines a-b]" label
# words the quoting/rendering layers add around the real text
_FRAMING_WORDS = frozenset("user assistant human system developer tool said instruction withheld text like".split())


def _tokens(text: object) -> frozenset[str]:
    return frozenset(t for t in recall_format.tokens(text) if not t.isdigit())


BOILERPLATE = _tokens(" ".join([
    RECALL_PREFIX, *RECALL_HEADER, *transcript_fallback.HEADER, residual.HEADER, *recall_format.HEADER_LINES,
    *residual.KINDS.values(),
])) | _FRAMING_WORDS


# --- block parsing ------------------------------------------------------------------------------------------------

def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n\n".join(
            part if isinstance(part, str) else str(part.get("text", ""))
            for part in content if isinstance(part, (str, dict))
        )
    return ""


def split_block(text: str) -> list[dict]:
    """Sections of an injected attachment: ``{"claims": [(id, text)], "excerpts": [text], "residual": [text]}`` pieces."""
    sections: list[tuple[str, list[str]]] = []
    for line in text.split("\n"):
        if line.startswith(RECALL_MARK):
            sections.append(("recall", []))
        elif line.startswith(RESIDUAL_MARK):
            sections.append(("residual", []))
        elif line.startswith("[MemoryMaster "):
            sections.append(("other", []))
        elif sections:
            sections[-1][1].append(line)
    out = []
    for kind, lines in sections:
        if kind == "residual":
            out.append({"claims": [], "excerpts": [], "residual": ["\n".join(lines)]})
        elif kind == "recall":
            claims_lines: list[str] = []
            excerpts: list[str] = []
            inside = False
            for line in lines:
                if line.strip() == transcript_fallback.OPEN_TAG:
                    inside = True
                elif line.strip() == transcript_fallback.CLOSE_TAG:
                    inside = False
                elif inside:
                    excerpts.append(_LABEL.sub("", line))
                elif line not in transcript_fallback.HEADER:
                    claims_lines.append(line)
            _, bullets = recall_format.parse_context("\n".join(claims_lines))
            claims = [(m.group(1), _BULLET_ID.sub("", body)) for cid, body in bullets if (m := _BULLET_ID.match(body))]
            out.append({"claims": claims, "excerpts": excerpts, "residual": []})
    return out


def block_units(sections: list[dict], prompt_keys: frozenset[str]) -> dict:
    """Distinctive word sets per part, the per-claim word sets, and the injected ids of one attachment."""
    drop = prompt_keys | BOILERPLATE
    words: dict[str, set[str]] = {"claims": set(), "excerpts": set(), "residual": set()}
    claims: list[tuple[str, frozenset[str]]] = []
    for sec in sections:
        for cid, body in sec["claims"]:
            keys = _tokens(body) - drop
            claims.append((cid, keys))
            words["claims"] |= keys
        for part in ("excerpts", "residual"):
            for body in sec[part]:
                words[part] |= _tokens(body) - drop
    words["all"] = words["claims"] | words["excerpts"] | words["residual"]
    present = {p for p in ("claims", "excerpts", "residual") if any(sec[p] for sec in sections)}
    return {"words": words, "present": present, "claims": claims}


# --- accumulation -------------------------------------------------------------------------------------------------

class Tally:
    """Counts for one (scope, part): observed vs shuffled, any-use and strong-use, citation."""

    def __init__(self) -> None:
        self.c: Counter = Counter()

    def add(self, nwords: int, used_obs: int, cited_obs: bool, has_ids: bool,
            controls: list[tuple[int, bool]], min_words: int) -> None:
        c = self.c
        c["units"] += 1
        if nwords >= 1:
            c["any_elig"] += 1
            c["any_obs"] += used_obs >= 1
            c["any_ctl_pairs"] += len(controls)
            c["any_ctl"] += sum(u >= 1 for u, _ in controls)
        if nwords >= min_words:
            c["strong_elig"] += 1
            c["strong_obs"] += used_obs >= min_words
            c["strong_ctl_pairs"] += len(controls)
            c["strong_ctl"] += sum(u >= min_words for u, _ in controls)
        if has_ids:
            c["cite_elig"] += 1
            c["cite_obs"] += cited_obs
            c["cite_ctl_pairs"] += len(controls)
            c["cite_ctl"] += sum(ci for _, ci in controls)

    def out(self) -> dict:
        c = self.c
        d = {k: int(c[k]) for k in (
            "units", "any_elig", "any_obs", "any_ctl_pairs", "any_ctl", "strong_elig", "strong_obs",
            "strong_ctl_pairs", "strong_ctl", "cite_elig", "cite_obs", "cite_ctl_pairs", "cite_ctl")}
        for name in ("any", "strong", "cite"):
            elig, pairs = d[f"{name}_elig"], d[f"{name}_ctl_pairs"]
            d[f"{name}_rate_observed"] = round(d[f"{name}_obs"] / elig, 4) if elig else None
            d[f"{name}_rate_shuffled"] = round(d[f"{name}_ctl"] / pairs, 4) if pairs else None
        return d


class Acc:
    def __init__(self, min_words: int) -> None:
        self.min_words = min_words
        self.overall = {p: Tally() for p in PARTS}
        self.by_project: dict[str, dict[str, Tally]] = {}
        self.claim_ids: dict[str, list[int]] = {}  # id -> [injected, used]
        self.counts: Counter = Counter()

    def unit(self, project: str, blk: dict, answer: frozenset[str], cited: set[str], controls: list[tuple]) -> None:
        """``controls`` = [(answer keys, cited ids)] of the shuffled partners."""
        ids = {cid for cid, _ in blk["claims"]}
        proj = self.by_project.setdefault(project, {p: Tally() for p in PARTS})
        for part in PARTS:
            if part != "all" and part not in blk["present"]:
                continue
            words = blk["words"][part]
            has_ids = bool(ids) and part in ("claims", "all")
            ctl = [(len(words & ans), bool(ids & ci)) for ans, ci in controls]
            for tally in (self.overall[part], proj[part]):
                tally.add(len(words), len(words & answer), bool(ids & cited), has_ids, ctl, self.min_words)

    def claim_usage(self, blk: dict, answer: frozenset[str], cited: set[str]) -> None:
        for cid, keys in blk["claims"]:
            if not keys and cid not in cited:
                self.counts["claim_lines_covered_by_prompt"] += 1
                continue
            row = self.claim_ids.setdefault(cid, [0, 0])
            row[0] += 1
            row[1] += cid in cited or len(keys & answer) >= min(self.min_words, len(keys))

    def report(self) -> dict:
        never = sorted(((n, cid) for cid, (n, used) in self.claim_ids.items() if not used), key=lambda t: (-t[0], t[1]))
        return {
            "overall": {p: t.out() for p, t in self.overall.items()},
            "by_project": {
                name: {p: t.out() for p, t in parts.items() if t.c["units"]}
                for name, parts in sorted(self.by_project.items(), key=lambda kv: -kv[1]["all"].c["units"])
            },
            "claims": {
                "ids_injected": len(self.claim_ids),
                "ids_never_used": len(never),
                "injections_total": sum(n for n, _ in self.claim_ids.values()),
                "top_never_used": [{"id": cid, "injected": n} for n, cid in never[:20]],
            },
        }


# --- transcript streaming -----------------------------------------------------------------------------------------

def _lines(path: Path, counts: Counter):
    with open(path, encoding="utf-8", errors="replace") as fh:
        while True:
            line = fh.readline(MAX_LINE)
            if not line:
                return
            if len(line) == MAX_LINE and not line.endswith("\n"):
                while (rest := fh.readline(MAX_LINE)) and not rest.endswith("\n"):
                    pass
                counts["oversize_lines"] += 1
                continue
            yield line


def project_of(cwd: object) -> str:
    parts = [p for p in re.split(r"[\\/]", cwd.rstrip("\\/")) if p] if isinstance(cwd, str) else []
    return parts[-1][:40] if parts else "(unknown)"


def _prompt_text(rec: dict) -> str | None:
    """Text of a real user prompt; ``None`` for a tool result, a meta record or an empty message."""
    msg = rec.get("message")
    if rec.get("isMeta") or not isinstance(msg, dict):
        return None
    content = msg.get("content")
    if isinstance(content, list) and any(isinstance(p, dict) and p.get("type") == "tool_result" for p in content):
        return None
    text = _content_text(content if isinstance(content, str) else [p for p in content or [] if
                                                                     isinstance(p, dict) and p.get("type") == "text"])
    return text if text.strip() else None


def _assistant_text(rec: dict) -> str:
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(p.get("text", "")) for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


class _Turn:
    __slots__ = ("prompt_keys", "blocks", "answer", "cited", "project", "chars")

    def __init__(self, text: str, project: str) -> None:
        self.prompt_keys = _tokens(text[:PROMPT_CAP])
        self.blocks: list[dict] = []
        self.answer: list[str] = []
        self.cited: set[str] = set()
        self.project = project
        self.chars = 0

    def add_answer(self, text: str) -> None:
        if text and self.chars < ANSWER_CAP:
            self.answer.append(text[: ANSWER_CAP - self.chars])
            self.chars += len(text)
            self.cited.update(_MM_ID.findall(text))


def _reservoir(pool: list, seen: int, item: object, cap: int, rng: random.Random) -> None:
    if len(pool) < cap:
        pool.append(item)
    elif (j := rng.randrange(seen)) < cap:
        pool[j] = item


def analyze(root: Path, *, since: str | None = None, project: str | None = None, min_words: int = 3,
            seed: int = 0, controls: int = 5, max_files: int | None = None) -> dict:
    files = sorted(
        (p for d in sorted(root.iterdir()) if d.is_dir() for p in d.glob("*.jsonl")),
        key=lambda p: (-p.stat().st_mtime, p.name),
    ) if root.is_dir() else []
    counts: Counter = Counter(files_found=len(files))
    if since:
        cutoff = _day_epoch(since)
        files = [p for p in files if p.stat().st_mtime >= cutoff]
        counts["files_before_since"] = counts["files_found"] - len(files)
    if max_files is not None and len(files) > max_files:
        counts["files_capped_out"] = len(files) - max_files
        files = files[:max_files]
    acc = Acc(min_words)
    acc.counts = counts
    seen_attachments: set[str] = set()
    project_pool: dict[str, list] = {}
    project_seen: Counter = Counter()
    deferred: list[tuple] = []  # blocks whose session has no other answered turn: paired with the project pool
    rng = random.Random(seed)
    for file_idx, path in enumerate(files):
        try:
            _scan_file(path, file_idx, acc, since, project, controls, seed, seen_attachments, project_pool,
                       project_seen, deferred, rng)
            counts["files_read"] += 1
        except OSError:
            counts["files_unreadable"] += 1
    for file_idx, proj, blk, answer, cited in deferred:
        pool = [e for e in project_pool.get(proj, []) if e[0] != file_idx]
        partners = random.Random(f"{seed}:{proj}:{len(pool)}:{len(blk['claims'])}").sample(pool, min(controls, len(pool)))
        if partners:
            acc.unit(proj, blk, answer, cited, [(e[1], e[2]) for e in partners])
        else:
            counts["units_without_control"] += 1
    return {
        "schema": 1,
        "params": {"since": since, "project": project, "min_words": min_words, "seed": seed, "controls": controls,
                   "max_files": max_files},
        "controls_applied": [
            "prompt words removed from the block before counting",
            "block framing (headers, labels, ids, dates) removed",
            f"shuffled: each block scored against up to {controls} answers of other turns (same session, else project)",
        ],
        "input": {k: int(v) for k, v in sorted(counts.items())},
        **acc.report(),
    }


def _day_epoch(day: str) -> float:
    from datetime import datetime, timezone
    return datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()


def _scan_file(path: Path, file_idx: int, acc: Acc, since: str | None, project_filter: str | None, controls: int,
               seed: int, seen_attachments: set[str], project_pool: dict, project_seen: Counter, deferred: list,
               rng: random.Random) -> None:
    counts = acc.counts
    turns: list[tuple[_Turn, frozenset[str]]] = []  # finished turns that have an answer: (turn, answer keys)
    pool: list[tuple[int, frozenset[str], set[str]]] = []  # (turn index, answer keys, cited ids)
    answered = 0
    cur: _Turn | None = None
    cwd = None

    def finish() -> None:
        nonlocal answered
        if cur is None:
            return
        keys = _tokens("\n".join(cur.answer))
        idx = len(turns)
        if keys:
            answered += 1
            _reservoir(pool, answered, (idx, keys, cur.cited), POOL_CAP, rng)
            proj = cur.project
            project_seen[proj] += 1
            _reservoir(project_pool.setdefault(proj, []), project_seen[proj], (file_idx, keys, cur.cited),
                       PROJECT_POOL_CAP, rng)
        elif cur.blocks:
            counts["blocks_in_turns_without_answer"] += len(cur.blocks)
        turns.append((cur, keys))

    for line in _lines(path, counts):
        if line.startswith('{"type":') or '"isSidechain":true' in line:
            continue
        m = _KIND.search(line)
        if not m:
            continue
        kind = m.group(1)
        if kind == "user" and '"type":"tool_result"' in line:
            continue
        if kind == "assistant" and '"type":"text"' not in line:
            continue
        if kind == "attachment" and ("hook_additional_context" not in line or "[MemoryMaster " not in line):
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            counts["malformed_lines"] += 1
            continue
        if not isinstance(rec, dict) or rec.get("isSidechain"):
            continue
        ts = rec.get("timestamp")
        if since and isinstance(ts, str) and ts[:10] < since:
            continue
        cwd = rec.get("cwd") or cwd
        if kind == "user":
            text = _prompt_text(rec)
            if text is None:
                continue
            finish()
            cur = _Turn(text, project_of(cwd))
        elif kind == "assistant":
            if cur is not None:
                cur.add_answer(_assistant_text(rec))
        else:
            att = rec.get("attachment")
            if cur is None or not isinstance(att, dict) or att.get("hookEvent") != "UserPromptSubmit":
                continue
            sections = split_block(_content_text(att.get("content")))
            if not sections:
                continue
            uid = rec.get("uuid")
            if isinstance(uid, str):
                if uid in seen_attachments:
                    counts["duplicate_blocks_skipped"] += 1
                    continue
                seen_attachments.add(uid)
            cur.project = project_of(rec.get("cwd") or cwd)
            if project_filter and cur.project != project_filter:
                continue
            cur.blocks.append(block_units(sections, cur.prompt_keys))
    finish()

    for idx, (turn, keys) in enumerate(turns):
        if not turn.blocks or not keys or (project_filter and turn.project != project_filter):
            continue
        for blk in turn.blocks:
            counts["blocks_analysed"] += 1
            acc.claim_usage(blk, keys, turn.cited)
            others = [e for e in pool if e[0] != idx]
            if not others:
                deferred.append((file_idx, turn.project, blk, keys, turn.cited))
                continue
            pick = random.Random(f"{seed}:{path.name}:{idx}").sample(others, min(controls, len(others)))
            acc.unit(turn.project, blk, keys, turn.cited, [(e[1], e[2]) for e in pick])


# --- output -------------------------------------------------------------------------------------------------------

def _pct(k: int, n: int) -> str:
    return f"{k}/{n} {100 * k / n:4.1f}%" if n else "-"


def render_table(rep: dict) -> str:
    inp = rep["input"]
    lines = [
        f"files read {inp.get('files_read', 0)} of {inp.get('files_found', 0)} "
        f"(unreadable {inp.get('files_unreadable', 0)}, capped out {inp.get('files_capped_out', 0)}, "
        f"older than --since {inp.get('files_before_since', 0)}); blocks analysed {inp.get('blocks_analysed', 0)}",
        "controls: prompt words and block framing removed; shuffled = answers of other turns",
        f"{'part':9} {'units':>6}  {'any observed':>16}  {'any shuffled':>16}  {'strong observed':>16}  "
        f"{'strong shuffled':>16}  {'cite obs':>12}  {'cite shuf':>12}",
    ]
    for part in PARTS:
        t = rep["overall"][part]
        lines.append(
            f"{part:9} {t['units']:>6}  {_pct(t['any_obs'], t['any_elig']):>16}  "
            f"{_pct(t['any_ctl'], t['any_ctl_pairs']):>16}  {_pct(t['strong_obs'], t['strong_elig']):>16}  "
            f"{_pct(t['strong_ctl'], t['strong_ctl_pairs']):>16}  {_pct(t['cite_obs'], t['cite_elig']):>12}  "
            f"{_pct(t['cite_ctl'], t['cite_ctl_pairs']):>12}")
    cl = rep["claims"]
    lines.append(f"claim ids injected {cl['ids_injected']}, never used {cl['ids_never_used']}; top never-used (id x injections):")
    lines.append("  " + ", ".join(f"{r['id']}x{r['injected']}" for r in cl["top_never_used"]))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", type=Path, default=Path.home() / ".claude" / "projects")
    ap.add_argument("--since", help="YYYY-MM-DD: only files modified and records stamped on/after this day")
    ap.add_argument("--project", help="last cwd segment to keep")
    ap.add_argument("--min-words", type=int, default=3, help="distinct block words for strong use (default 3)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--controls", type=int, default=5, help="shuffled partner answers per block (default 5)")
    ap.add_argument("--max-files", type=int, help="read only the N most recently modified files")
    ap.add_argument("--json", action="store_true", help="print the JSON document instead of the table")
    ap.add_argument("--out", type=Path, help="also write the JSON document here")
    args = ap.parse_args(argv)
    rep = analyze(args.root, since=args.since, project=args.project, min_words=max(1, args.min_words), seed=args.seed,
                  controls=max(1, args.controls), max_files=args.max_files)
    doc = json.dumps(rep, indent=2, sort_keys=True)
    if args.out:
        args.out.write_text(doc + "\n", encoding="utf-8")
    print(doc if args.json else render_table(rep))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Offline replay of the recall injection formats over recent Claude Code transcripts (T-1632, AO-0).

Read-only. Reuses the T-1343 transcript scan (``review_recall_citations``), keeps each prompt and the
injection it received in memory only, and re-renders that injection under every mode of
``memorymaster.recall.recall_format`` (current / header / gated). It never opens the claims database, never
calls recall and writes only counts, claim short ids and lane names: no prompt or claim text is emitted.

A replay can only ask "would the model have seen less, and would it still have seen what it cited?". It
cannot say whether a different header makes the model cite more: that is behaviour, measured live. The
active task of a past prompt is not recoverable, so ``header``/``gated`` rank by prompt overlap alone here.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # this checkout, not an installed copy
from scripts import review_recall_citations as rrc  # noqa: E402

from memorymaster.recall import recall_format as rf  # noqa: E402

MODES = rf.MODES
PREFIX = "[MemoryMaster recall]\n"
SWEEP = (1, 2, 3)


def tokens_of(chars: int) -> int:
    return (chars + 3) // 4  # same four-characters-per-token estimate recall uses to pack its budget


BRIEFING = "[Look-ahead briefing]"  # the installed hook appends this second block to the same payload


def block_context(block: str) -> str:
    """The recall claims of an injected payload: no prefix, no appended look-ahead briefing."""
    head = block.lstrip()
    head = head[len(PREFIX):] if head.startswith(PREFIX) else head
    return head.split(BRIEFING, 1)[0].rstrip()


def render(turn, mode: str) -> tuple[str, set[str]]:
    """Text the turn's blocks would have delivered under ``mode`` and the claim ids in it."""
    parts, ids = [], set()
    for block in turn.blocks:
        out = rf.apply_format(block_context(block), turn.prompt, "", mode)
        if out.strip():
            parts.append(PREFIX + out)
            ids.update(c for c, _ in rf.parse_context(out)[1])
    return "\n\n".join(parts), ids


def median(values):
    return statistics.median(values) if values else None


def share(num, den):
    return num / den if den else None


def summarize_mode(turns, mode: str, substantive: int) -> dict:
    kept_sizes, kept_prompts, kept_claims, cited_turns_kept, cited_claims_kept = [], 0, 0, 0, 0
    cited_turns = cited_claims = 0
    for turn in turns:
        if not turn.ids:
            continue
        text, ids = render(turn, mode)
        hit = turn.ids & turn.cited
        cited_turns += bool(hit)
        cited_claims += len(hit)
        if ids:
            kept_prompts += 1
            kept_claims += len(ids)
            kept_sizes.append(tokens_of(len(text)))
            kept_claims_hit = ids & turn.cited
            cited_turns_kept += bool(kept_claims_hit)
            cited_claims_kept += len(kept_claims_hit)
    return {
        "prompts_with_injection": kept_prompts,
        "injected_claims": kept_claims,
        "median_tokens": median(kept_sizes),
        "cited_prompts_total": cited_turns,
        "cited_prompts_still_injected": cited_turns_kept,
        "cited_claims_total": cited_claims,
        "cited_claims_still_injected": cited_claims_kept,
        "rate_per_claim": share(cited_claims_kept, kept_claims),
        "rate_per_injected_prompt": share(cited_turns_kept, kept_prompts),
        "rate_per_substantive_prompt": share(cited_turns_kept, substantive),
    }


def relevance(turns) -> dict:
    """Lexical-overlap proxy: how many injections contain no claim sharing a meaningful word with the prompt."""
    none = total = 0
    top_shared = {"cited": Counter(), "uncited": Counter()}
    for turn in turns:
        if not turn.ids:
            continue
        total += 1
        query = rf.tokens(turn.prompt)
        best, top = 0, 0
        for block in turn.blocks:
            _, bullets = rf.parse_context(block_context(block))
            for _, text in bullets:
                best = max(best, len(query & rf.tokens(text)))
            if bullets:
                top = max(top, len(query & rf.tokens(rf.ranked(bullets, turn.prompt, "")[0][4])))
        none += best == 0
        top_shared["cited" if turn.ids & turn.cited else "uncited"][min(top, 5)] += 1
    return {"injections": total, "no_shared_token": none, "share_no_shared_token": share(none, total),
            "top_claim_shared_tokens_histogram": {k: dict(sorted(v.items())) for k, v in top_shared.items()}}


def per_day(turns) -> dict:
    days: dict[str, dict] = {}
    for turn in turns:
        if not turn.ids or turn.ts is None:
            continue
        day = days.setdefault(turn.ts.date().isoformat(), {"prompts_with_injection": 0, "injected_claims": 0,
                                                            "cited_claims": 0})
        day["prompts_with_injection"] += 1
        day["injected_claims"] += len(turn.ids)
        day["cited_claims"] += len(turn.ids & turn.cited)
    for day in days.values():
        day["claims_per_injection"] = share(day["injected_claims"], day["prompts_with_injection"])
    return dict(sorted(days.items()))


def baseline(turns) -> dict:
    sizes = [tokens_of(sum(len(PREFIX) + len(block_context(b)) for b in turn.blocks)) for turn in turns if turn.ids]
    return {"median_injected_tokens": median(sizes), "p90_injected_tokens":
            (sorted(sizes)[int(len(sizes) * 0.9)] if sizes else None), "relevance": relevance(turns)}


def replay(home, mapping_path, days=7, now=None, since=None) -> dict:
    home = Path(home)
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    _, repos = rrc.load_lane_map(mapping_path)
    diagnostics = Counter()
    raw: dict = {}
    main_lanes, main_total, *_ = rrc.collect_transcripts(home, repos, cutoff, now, diagnostics, raw=raw)
    every = [t for turns in raw.values() for t in turns]
    out = {"generated_at": now.isoformat(), "window_days": days, "official_total": main_total,
           "lanes": {}, "total": {}, "gated_sweep": {}}

    def entry_for(turns):
        substantive = sum(1 for t in turns if t.real and t.prompt.strip())
        return {"substantive_prompts": substantive, "baseline": baseline(turns),
                "strict_prompts_with_citation": sum(1 for t in turns if t.ids & t.cited),
                "modes": {mode: summarize_mode(turns, mode, substantive) for mode in MODES}}

    out["total"] = entry_for(every)
    for lane, turns in sorted(raw.items()):
        if any(t.ids for t in turns):
            out["lanes"][lane] = entry_for(turns)
    if since is not None:  # e.g. only the days after the T-1351 two-claim cap was deployed
        later = {lane: [t for t in turns if t.ts is not None and t.ts >= since] for lane, turns in raw.items()}
        out["since"] = {"from": since.isoformat(), "total": entry_for([t for ts in later.values() for t in ts]),
                        "lanes": {lane: entry_for(ts) for lane, ts in sorted(later.items())
                                  if any(t.ids for t in ts)}}
    out["per_day"] = per_day(every)
    previous = os.environ.get(rf.GATE_MIN_SHARED_ENV)
    try:
        for n in SWEEP:
            os.environ[rf.GATE_MIN_SHARED_ENV] = str(n)
            out["gated_sweep"][str(n)] = summarize_mode(every, "gated", out["total"]["substantive_prompts"])
    finally:
        if previous is None:
            os.environ.pop(rf.GATE_MIN_SHARED_ENV, None)
        else:
            os.environ[rf.GATE_MIN_SHARED_ENV] = previous
    top = Counter()
    cited = Counter()
    for turn in every:
        top.update(turn.ids)
        cited.update(turn.ids & turn.cited)
    never = sorted(((i, n) for i, n in top.items() if not cited[i]), key=lambda x: (-x[1], x[0]))
    out["top10_injected_never_cited"] = [{"id": i, "injected": n} for i, n in never[:10]]
    out["official_script_agreement"] = {
        "prompts_with_injection": [main_total["prompts_with_injection"], sum(1 for t in every if t.ids)],
        "injected_claims": [main_total["injected_claims"], sum(len(t.ids) for t in every)],
        "cited_claims": [main_total["cited_claims"], sum(len(t.ids & t.cited) for t in every)],
    }
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--since", help="ISO time: also report the turns from then on (post-cap view)")
    parser.add_argument("--out", help="write the JSON here (default: stdout)")
    parser.add_argument("--home", default=str(Path.home()), help=argparse.SUPPRESS)
    parser.add_argument("--lanes", help="orchestrators.json (default: Py Apps/_intel/orchestrators.json)")
    args = parser.parse_args(argv)
    mapping = args.lanes
    if not mapping:
        for parent in Path(__file__).resolve().parents:
            if (parent / "_intel" / "orchestrators.json").exists():
                mapping = parent / "_intel" / "orchestrators.json"
                break
    if not mapping:
        parser.error("orchestrators.json not found; pass --lanes")
    text = json.dumps(replay(args.home, mapping, days=args.days, since=rrc.parse_time(args.since)), indent=2, sort_keys=True, default=str)
    if args.out:
        out = Path(args.out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
    else:
        sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Expand recall eval set from 30 → 100 real user prompts.

Reads raw Claude Code conversation transcripts (JSONL session files), extracts
user prompts, applies filters (length, slash-commands, system-reminders,
tool-result wrappers, secrets), dedupes against existing prompts, and samples
up to 100 distinct prompts total. Merges with the existing 30-prompt set to
preserve timestamps + source.

Also runs the recall hook (MEMORYMASTER_RECALL_VERBATIM=0) over every new
prompt, collects top-20 candidate claim IDs, and emits heuristic relevance
labels via token overlap (>=3 content tokens in common, non-stale/archived).

Output:
    artifacts/real-prompts-100.jsonl              (100 lines, {text, timestamp, source})
    artifacts/real-prompts-100-labels.json        (side-file, prompt_hash -> [claim_ids])

Usage:
    python scripts/expand_recall_eval.py
    python scripts/expand_recall_eval.py --transcripts-dir <path>
    python scripts/expand_recall_eval.py --target 100 --dedup-threshold 0.8
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from memorymaster.recall_tokenizer import _candidate_tokens  # noqa: E402
from memorymaster.security import redact_text  # noqa: E402

DEFAULT_TRANSCRIPTS = Path(
    os.path.expanduser(
        "~/.claude/projects/G---OneDrive-OneDrive-Desktop-Py-Apps-memorymaster"
    )
)
DEFAULT_EXISTING = REPO / "artifacts" / "real-prompts.jsonl"
DEFAULT_EXTRA = REPO / "artifacts" / "real-prompts-sessionopen.jsonl"
DEFAULT_OUT = REPO / "artifacts" / "real-prompts-100.jsonl"
DEFAULT_LABELS = REPO / "artifacts" / "real-prompts-100-labels.json"


# Patterns/markers to strip/skip — mirrors the in-transcript noise.
_SLASH_COMMANDS = ("/clear", "/continue", "/compact", "/new", "/help",
                   "/exit", "/quit", "/config", "/logout", "/cost",
                   "/model", "/memory")
_SYSTEM_TAGS = ("<local-command-caveat>", "<system-reminder>",
                "<command-name>", "<command-message>", "<command-args>",
                "<bash-input>", "<bash-stdout>", "<bash-stderr>",
                "<ide_selection>", "<ide_opened_file>")


@dataclass(frozen=True)
class PromptRecord:
    text: str
    timestamp: str | None
    source: str


def _sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _load_jsonl_prompts(path: Path) -> list[PromptRecord]:
    out: list[PromptRecord] = []
    if not path.exists():
        return out
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            text = (rec.get("text") or "").strip()
            if not text:
                continue
            out.append(PromptRecord(
                text=text,
                timestamp=rec.get("timestamp"),
                source=rec.get("source") or path.name,
            ))
    return out


def _extract_text_from_content(content: object) -> str:
    """Pull plain user text out of a message.content payload.

    Message.content can be:
      - str: raw text
      - list of dicts with {type, text} or {type: "tool_result", ...}
    We ignore tool_result items entirely.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text" and isinstance(item.get("text"), str):
                parts.append(item["text"])
            # Skip tool_result, tool_use, image, etc.
        return "\n".join(parts).strip()
    return ""


def _is_noise(text: str) -> bool:
    """Drop transcript wrappers and slash-commands."""
    stripped = text.strip()
    if not stripped:
        return True
    lower = stripped.lower()
    for tag in _SYSTEM_TAGS:
        if tag in lower:
            return True
    # Slash commands: exact or prefix match on first non-whitespace token.
    first = stripped.split(None, 1)[0].lower() if stripped else ""
    if first in _SLASH_COMMANDS:
        return True
    # /project-* /wiki:* /ask:* etc.
    if first.startswith("/") and len(first) <= 32 and " " not in first:
        return True
    # "Caveat:" prefix from local-command-caveat (already handled by tag, kept as belt+braces)
    if lower.startswith("caveat:"):
        return True
    return False


def _iter_transcript_prompts(transcripts_dir: Path) -> Iterable[PromptRecord]:
    """Walk all *.jsonl files under the transcripts dir and yield user prompts."""
    for path in sorted(transcripts_dir.glob("*.jsonl")):
        try:
            with path.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("type") != "user":
                        continue
                    msg = obj.get("message")
                    if not isinstance(msg, dict) or msg.get("role") != "user":
                        continue
                    text = _extract_text_from_content(msg.get("content")).strip()
                    if not text:
                        continue
                    yield PromptRecord(
                        text=text,
                        timestamp=obj.get("timestamp"),
                        source=path.name,
                    )
        except OSError:
            continue


def _token_set(text: str) -> set[str]:
    """Content-token set — same filter as the recall tokenizer."""
    return {t for t in _candidate_tokens(text) if len(t) >= 3}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _passes_filters(rec: PromptRecord,
                    min_len: int = 10,
                    max_len: int = 1000) -> tuple[bool, str]:
    text = rec.text.strip()
    n = len(text)
    if n < min_len:
        return False, "too_short"
    if n > max_len:
        return False, "too_long"
    if _is_noise(text):
        return False, "noise"
    # Sensitivity filter — last line of defense.
    redacted, findings = redact_text(text)
    if findings or redacted != text:
        return False, f"sensitive:{','.join(findings) or 'diff'}"
    return True, "ok"


def _dedup(existing_tokens: list[set[str]],
           rec: PromptRecord,
           threshold: float = 0.8,
           seen_exact: set[str] | None = None) -> bool:
    """Return True if rec is a near-duplicate of any prior record."""
    if seen_exact is not None:
        norm = " ".join(rec.text.split()).lower()
        if norm in seen_exact:
            return True
    toks = _token_set(rec.text)
    if not toks:
        return True  # no meaningful content == dedup
    for prior in existing_tokens:
        if _jaccard(toks, prior) >= threshold:
            return True
    return False


# -- Ground-truth labelling ---------------------------------------------------

def _label_prompts(new_records: list[PromptRecord],
                   db_path: str,
                   top_k: int = 20,
                   min_overlap: int = 3) -> dict[str, list[int]]:
    """Run recall over each prompt, return heuristic-relevant claim IDs.

    Sets MEMORYMASTER_RECALL_VERBATIM=0 for the duration to isolate the
    classic stream (matches the spec).
    """
    # Lazy imports — the eval harness already knows how to collect candidates.
    from scripts.eval_recall_precision_at_5 import _fetch_candidates  # type: ignore
    from memorymaster.service import MemoryService

    prior = os.environ.get("MEMORYMASTER_RECALL_VERBATIM")
    os.environ["MEMORYMASTER_RECALL_VERBATIM"] = "0"
    try:
        svc = MemoryService(db_target=db_path, workspace_root=REPO)
        # Read-only hardening
        svc._record_accesses = lambda *a, **k: None  # type: ignore[assignment]
        if hasattr(svc, "store") and hasattr(svc.store, "record_accesses_batch"):
            svc.store.record_accesses_batch = lambda *a, **k: None  # type: ignore[assignment]

        labels: dict[str, list[int]] = {}
        for rec in new_records:
            rows = _fetch_candidates(svc, rec.text, db_path, top_k=top_k,
                                     include_entity_fanout=True,
                                     include_vector_fallback=False)
            p_toks = _token_set(rec.text)
            relevant: list[int] = []
            for row in rows:
                claim = row.get("claim")
                cid = getattr(claim, "id", None)
                status = getattr(claim, "status", "") or ""
                if cid is None or status in ("stale", "archived"):
                    continue
                c_toks = _token_set(
                    f"{getattr(claim, 'subject', '') or ''} {getattr(claim, 'text', '') or ''}"
                )
                if len(p_toks & c_toks) >= min_overlap:
                    relevant.append(cid)
            labels[_sha(rec.text)] = relevant
        return labels
    finally:
        if prior is None:
            os.environ.pop("MEMORYMASTER_RECALL_VERBATIM", None)
        else:
            os.environ["MEMORYMASTER_RECALL_VERBATIM"] = prior


def _write_jsonl(path: Path, records: list[PromptRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            row = {"text": rec.text, "source": rec.source}
            if rec.timestamp:
                row["timestamp"] = rec.timestamp
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--transcripts-dir", default=str(DEFAULT_TRANSCRIPTS))
    ap.add_argument("--existing", default=str(DEFAULT_EXISTING))
    ap.add_argument("--extra", default=str(DEFAULT_EXTRA),
                    help="Additional existing prompts file to seed from")
    ap.add_argument("--output", default=str(DEFAULT_OUT))
    ap.add_argument("--labels-output", default=str(DEFAULT_LABELS))
    ap.add_argument("--db", default=str(REPO / "memorymaster.db"))
    ap.add_argument("--target", type=int, default=100,
                    help="Total prompts to emit")
    ap.add_argument("--dedup-threshold", type=float, default=0.8,
                    help="Jaccard token-overlap threshold for near-dup rejection")
    ap.add_argument("--min-len", type=int, default=10)
    ap.add_argument("--max-len", type=int, default=1000)
    ap.add_argument("--min-overlap", type=int, default=3,
                    help="Min token overlap for a claim to be 'relevant'")
    ap.add_argument("--no-labels", action="store_true",
                    help="Skip ground-truth labelling (fast mode)")
    ap.add_argument("--seed", type=int, default=20260423)
    args = ap.parse_args()

    transcripts_dir = Path(args.transcripts_dir)
    if not transcripts_dir.exists():
        print(f"ERROR: transcripts dir missing: {transcripts_dir}")
        return 2

    existing_path = Path(args.existing)
    extra_path = Path(args.extra)

    existing = _load_jsonl_prompts(existing_path)
    extras = _load_jsonl_prompts(extra_path)
    print(f"Existing seed: {len(existing)} from {existing_path.name}")
    print(f"Extra seed:    {len(extras)} from {extra_path.name}")

    # Build "already-have" set: normalize text for exact dedup, token-sets for
    # near-dup.
    sampled: list[PromptRecord] = list(existing)
    seen_exact: set[str] = set()
    tokens_accum: list[set[str]] = []
    for rec in sampled:
        seen_exact.add(" ".join(rec.text.split()).lower())
        tokens_accum.append(_token_set(rec.text))

    # Folder in the extras that aren't already in `existing`.
    for rec in extras:
        if len(sampled) >= args.target:
            break
        ok, why = _passes_filters(rec, args.min_len, args.max_len)
        if not ok:
            continue
        if _dedup(tokens_accum, rec, args.dedup_threshold, seen_exact):
            continue
        sampled.append(rec)
        seen_exact.add(" ".join(rec.text.split()).lower())
        tokens_accum.append(_token_set(rec.text))

    scanned = 0
    kept_after_filter = 0
    kept_after_dedup = 0
    reject_reasons: dict[str, int] = {}

    # Walk transcripts.
    for rec in _iter_transcript_prompts(transcripts_dir):
        scanned += 1
        if len(sampled) >= args.target:
            continue
        ok, why = _passes_filters(rec, args.min_len, args.max_len)
        if not ok:
            reject_reasons[why] = reject_reasons.get(why, 0) + 1
            continue
        kept_after_filter += 1
        if _dedup(tokens_accum, rec, args.dedup_threshold, seen_exact):
            reject_reasons["near_dup"] = reject_reasons.get("near_dup", 0) + 1
            continue
        kept_after_dedup += 1
        sampled.append(rec)
        seen_exact.add(" ".join(rec.text.split()).lower())
        tokens_accum.append(_token_set(rec.text))

    print()
    print(f"Scanned user prompts:   {scanned}")
    print(f"Passed filters:          {kept_after_filter}")
    print(f"Passed dedup:            {kept_after_dedup}")
    print(f"Total sampled:           {len(sampled)} (target={args.target})")
    print("Reject reasons:")
    for k, v in sorted(reject_reasons.items(), key=lambda kv: -kv[1])[:10]:
        print(f"  {k}: {v}")

    out_path = Path(args.output)
    _write_jsonl(out_path, sampled)
    print(f"\nWrote {len(sampled)} prompts → {out_path}")

    # Labels — only label the NEW ones (existing 30 already have auto labels
    # via the eval harness's token-overlap proxy).
    if not args.no_labels:
        new_only = sampled[len(existing):]
        print(f"\nLabelling {len(new_only)} new prompts "
              f"(MEMORYMASTER_RECALL_VERBATIM=0, top-20, min_overlap={args.min_overlap})...")
        labels = _label_prompts(new_only, str(Path(args.db)),
                                 top_k=20, min_overlap=args.min_overlap)
        rel_counts = [len(v) for v in labels.values()]
        mean_rel = sum(rel_counts) / max(1, len(rel_counts))
        print(f"  mean relevant claims/prompt = {mean_rel:.2f}")
        print(f"  prompts with 0 relevant      = {sum(1 for c in rel_counts if c == 0)}")
        labels_path = Path(args.labels_output)
        labels_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "min_overlap": args.min_overlap,
            "top_k": 20,
            "labels": labels,
            "note": ("LLM-free heuristic labels. A candidate claim is marked "
                     "'relevant' when its (subject+text) shares >=min_overlap "
                     "content tokens with the prompt AND status != stale/archived. "
                     "Human spot-check recommended on ~10 samples."),
        }
        with labels_path.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        print(f"  Wrote labels → {labels_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Compare claim recall with dense and reranked retrieval over raw Claude history.

The question manifest contains only sanitized text, opaque transcript IDs and
message indexes. Raw transcripts and the SQLite authority are read-only. All
indexes and detailed run output belong in a caller-selected scratch directory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SECRET = re.compile(r"(?i)(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{20,}|Bearer\s+\S+|\b\d{1,3}(?:\.\d{1,3}){3}\b|[A-Z]:[\\/]|/Users/|/home/|\\\\[^\\]+\\)")
CHUNK_MESSAGES = 8
TOP_K = 5


def _clean(text: str) -> str:
    text = SECRET.sub("[REDACTED]", text)
    return " ".join(text.split())


def _ro(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db.resolve().as_posix()}?mode=ro", uri=True)
    con.execute("PRAGMA query_only=ON")
    return con


def _transcript_id(path: Path, root: Path) -> str:
    return hashlib.sha256(path.relative_to(root).as_posix().encode()).hexdigest()[:16]


def _records(path: Path) -> list[tuple[int, str, str]]:
    for ix, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines()):
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = item.get("message", {})
        role = msg.get("role") or item.get("type", "")
        body = msg.get("content", item.get("content", ""))
        if isinstance(body, list):
            body = "\n".join(str(x.get("text", "")) for x in body if isinstance(x, dict) and x.get("type") == "text")
        if role in {"user", "assistant"} and isinstance(body, str) and body.strip():
            yield ix, role, body


def _chunks(root: Path, days: int = 7) -> list[dict[str, Any]]:
    cutoff = time.time() - days * 86400
    chunks = []
    for path in sorted(root.glob("*/*.jsonl")):
        try:
            if path.stat().st_mtime < cutoff:
                continue
            records = list(_records(path))
        except OSError:
            continue
        sid = _transcript_id(path, root)
        for offset in range(0, len(records), CHUNK_MESSAGES):
            window = records[offset:offset + CHUNK_MESSAGES]
            text = _clean("\n".join(f"{role}: {body}" for _, role, body in window))
            if len(text) >= 80:
                chunks.append({"sid": sid, "first": window[0][0], "last": window[-1][0], "text": text})
    return chunks


def _call(cmd: list[str], prompt: str, timeout: int = 180) -> str:
    executable = shutil.which(cmd[0]) or shutil.which(cmd[0] + ".cmd")
    if executable and executable.lower().endswith((".cmd", ".bat")):
        cmd = ["cmd.exe", "/d", "/s", "/c", subprocess.list2cmdline([executable, *cmd[1:]])]
    p = subprocess.run(cmd, input=prompt, text=True, capture_output=True, timeout=timeout)
    if p.returncode:
        raise RuntimeError(f"{Path(cmd[0]).name} exited {p.returncode}")
    return p.stdout.strip()


def build(args: argparse.Namespace) -> None:
    """Ask the configured candidate model, then require exact-source validation."""
    import random

    root = args.transcripts.expanduser()
    chunks = _chunks(root)
    if len(chunks) < 100:
        raise SystemExit("fewer than 100 usable recent transcript chunks")
    random.Random(args.seed).shuffle(chunks)
    paths = {_transcript_id(p, root): p for p in root.glob("*/*.jsonl")}
    approved = []
    seen: set[str] = set()
    rej = dict.fromkeys(("no_evidence", "short", "secret", "overlap", "parse"), 0)
    for start in range(0, min(len(chunks), 8 * 50), 50):  # up to 8 disjoint batches
        if len(approved) >= 40:
            break
        sample = chunks[start:start + 50]
        payload = [{"sid": c["sid"], "first": c["first"], "last": c["last"], "text": c["text"][:5000]} for c in sample]
        prompt = ("Create 40 useful future-agent questions from this JSON. Return only a JSON array. "
                  "Every object must have question, answer, evidence_sid, evidence_message_index, category. "
                  "Evidence index must be one of the supplied JSONL line indexes and directly support answer. "
                  "Use category single-fact or relational. Exclude secrets, credentials, IPs, personal names/paths, "
                  "and anything not safe for a public repository. Prefer durable decisions, causes and gotchas.\n" + json.dumps(payload))
        output = _call([args.opencode, "run", "--standalone", "--format", "json", "-m", args.candidate_model], prompt, timeout=1200)
        # OpenCode JSON mode may emit JSON event lines; accept a direct array or the final text payload.
        try:
            candidates = json.loads(output)
        except json.JSONDecodeError:
            events = [json.loads(line) for line in output.splitlines() if line.lstrip().startswith("{")]
            texts = [e.get("text") or e.get("part", {}).get("text", "") for e in events]
            candidates = json.loads(next((t for t in reversed(texts) if t.strip().startswith("[")), "[]"))
        source = {(c["sid"], ix): body for c in sample for ix, _, body in _records(paths[c["sid"]])}
        def _toks(t: str) -> set[str]:
            return {w.lower() for w in re.findall(r"[A-Za-z0-9_-]{4,}", t)}

        for item in candidates:
            try:
                sid, ix = str(item["evidence_sid"]), int(item["evidence_message_index"])
                q, answer = _clean(str(item["question"])), _clean(str(item["answer"]))
                category = item["category"]
            except (KeyError, TypeError, ValueError):
                rej["parse"] += 1
                continue
            words = _toks(answer)
            # Accept the cited index or any within +-3 in the same session; keep the best answer-token overlap.
            near = [(len(words & _toks(source[(sid, j)])), -abs(j - ix), j) for j in range(ix - 3, ix + 4) if (sid, j) in source]
            if not near:
                rej["no_evidence"] += 1
            elif len(q) < 12 or len(answer) < 3:
                rej["short"] += 1
            elif SECRET.search(q + answer):
                rej["secret"] += 1
            elif q.lower() in seen:
                continue
            else:
                hits, _, best = max(near)
                # Candidate must quote no invented answer: require meaningful answer-token support.
                if hits < min(2, len(words)):
                    rej["overlap"] += 1
                    continue
                seen.add(q.lower())
                approved.append({"id": f"q{len(approved)+1:02}", "question": q, "answer": answer,
                                 "category": category if category in {"single-fact", "relational"} else "single-fact",
                                 "evidence": {"session": sid, "message_index": best}})
        print(json.dumps({"batch": start // 50 + 1, "approved": len(approved), "rejected": rej}), file=sys.stderr)
    print(json.dumps({"final_approved": len(approved), "rejected": rej}), file=sys.stderr)
    if len(approved) < 25:
        raise SystemExit(f"only {len(approved)} candidates passed source and privacy checks; curate more")
    if len(approved) < 30:
        print(f"warning: only {len(approved)} approved (brief target 30)", file=sys.stderr)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"seed": args.seed, "questions": approved[:40]}, indent=2), encoding="utf-8")


def _load_models():
    from sentence_transformers import SentenceTransformer, CrossEncoder
    return (SentenceTransformer("all-MiniLM-L6-v2", local_files_only=True),
            CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2", model_kwargs={"local_files_only": True}))


def _raw_retrieve(question: str, chunks: list[dict[str, Any]], models: tuple[Any, Any], vectors: Any) -> list[dict[str, Any]]:
    import numpy as np
    embedder, reranker = models
    q = embedder.encode([question], normalize_embeddings=True, show_progress_bar=False)[0]
    sims = np.asarray(vectors) @ np.asarray(q)
    shortlist = np.argsort(-sims)[:50]
    pairs = [(question, chunks[int(i)]["text"][:4000]) for i in shortlist]
    scores = reranker.predict(pairs, show_progress_bar=False)
    ranked = sorted(zip(scores, shortlist), reverse=True)
    return [chunks[int(i)] for _, i in ranked[:TOP_K]]


def _claim_retrieve(db: Path, query: str) -> tuple[list[dict[str, Any]], float]:
    from memorymaster.core.service import MemoryService
    start = time.perf_counter()
    service = MemoryService(db, workspace_root=ROOT, read_only=True)
    if not service.store.read_only:
        raise RuntimeError("refusing non-read-only claim store")
    rows = service.query_rows(query, limit=TOP_K, retrieval_mode="legacy", record_accesses=False)
    elapsed = (time.perf_counter() - start) * 1000
    out = []
    for row in rows[:TOP_K]:
        claim = row.get("claim", {})
        out.append({"text": str(getattr(claim, "text", "")),
                    "evidence": [getattr(c, "to_dict", lambda: {})() for c in getattr(claim, "citations", [])]})
    return out, elapsed


def _invoke_claude(prompt: str, model: str) -> tuple[str, float]:
    start = time.perf_counter()
    text = _call(["claude", "-p", "--model", model], prompt)
    return text, (time.perf_counter() - start) * 1000


def run(args: argparse.Namespace) -> None:
    data = json.loads(args.questions.read_text(encoding="utf-8"))["questions"]
    chunks = _chunks(args.transcripts.expanduser())
    if not args.db.exists():
        raise SystemExit("database path not found")
    with _ro(args.db) as con:
        con.execute("SELECT 1").fetchone()
    models = _load_models()
    # Cache raw chunk embeddings once; rerank only the dense shortlist per query.
    vectors = models[0].encode([c["text"] for c in chunks], normalize_embeddings=True, show_progress_bar=True)
    records: list[dict[str, Any]] = []
    for item in data:
        q = item["question"]
        raw_start = time.perf_counter()
        raw = _raw_retrieve(q, chunks, models, vectors)
        raw_ms = (time.perf_counter() - raw_start) * 1000
        claim, claim_ms = _claim_retrieve(args.db, q)
        union = []
        seen = set()
        for i in range(max(len(claim), len(raw))):
            for pool in (claim, raw):
                if i < len(pool):
                    entry = pool[i]
                    key = (entry.get("text", ""), json.dumps(entry.get("evidence", []), sort_keys=True, default=str))
                    if key not in seen:
                        seen.add(key)
                        union.append(entry)
        contexts = {"a": claim, "b": raw[:TOP_K], "c": union[:TOP_K]}
        for arm, rows in contexts.items():
            ctx = "\n\n".join(str(x.get("text", ""))[:2500] for x in rows)
            answer, llm_ms = _invoke_claude("Answer only from the supplied context. If absent, say unknown.\nQUESTION: " + q + "\nCONTEXT:\n" + ctx, args.answer_model)
            judge, judge_ms = _invoke_claude("Judge whether the proposed answer correctly states the gold answer, using the gold only as the criterion. Reply exactly CORRECT or INCORRECT.\nGOLD: " + item["answer"] + "\nPROPOSED: " + answer, args.judge_model)
            hit = False
            gold = item["evidence"]
            for row in rows:
                for citation in row.get("evidence", []) if isinstance(row.get("evidence"), list) else []:
                    if isinstance(citation, dict) and (str(citation.get("source", "")).find(gold["session"]) >= 0 or str(citation.get("message_index", "")) == str(gold["message_index"])):
                        hit = True
            if arm in {"b", "c"}:
                hit = hit or any(r.get("sid") == gold["session"] and r.get("first", 999999) <= gold["message_index"] <= r.get("last", -1) for r in rows)
            records.append({"id": item["id"], "category": item["category"], "arm": arm, "hit": hit,
                            "correct": judge.strip().upper().startswith("CORRECT"), "retrieval_ms": claim_ms if arm == "a" else raw_ms,
                            "llm_ms": llm_ms + judge_ms, "context_tokens": sum(max(1, len(x.get("text", "")) // 4) for x in rows)})
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "details.json").write_text(json.dumps(records, indent=2), encoding="utf-8")
    print(json.dumps(_summary(records), indent=2))


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    summary = {}
    for arm in "abc":
        group = [r for r in rows if r["arm"] == arm]
        summary[arm] = {"n": len(group), "hit_at_5": round(sum(r["hit"] for r in group) / max(len(group), 1), 3),
                        "correctness": round(sum(r["correct"] for r in group) / max(len(group), 1), 3),
                        "retrieval_ms_mean": round(sum(r["retrieval_ms"] for r in group) / max(len(group), 1), 1),
                        "context_tokens_mean": round(sum(r["context_tokens"] for r in group) / max(len(group), 1), 1),
                        "by_category": {cat: {"n": sum(x["category"] == cat for x in group),
                                              "hit_at_5": round(sum(x["hit"] for x in group if x["category"] == cat) / max(1, sum(x["category"] == cat for x in group)), 3),
                                              "correctness": round(sum(x["correct"] for x in group if x["category"] == cat) / max(1, sum(x["category"] == cat for x in group)), 3)} for cat in ("single-fact", "relational")}}
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(required=True)
    p = sub.add_parser("build")
    p.add_argument("--transcripts", type=Path, default=Path.home() / ".claude" / "projects")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--seed", type=int, default=1817)
    p.add_argument("--opencode", default="opencode")
    p.add_argument("--candidate-model", default="opencode-go/deepseek-v4.1-flash")
    p.set_defaults(func=build)
    p = sub.add_parser("run")
    p.add_argument("--transcripts", type=Path, default=Path.home() / ".claude" / "projects")
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--questions", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--answer-model", default="haiku")
    p.add_argument("--judge-model", default="haiku")
    p.set_defaults(func=run)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

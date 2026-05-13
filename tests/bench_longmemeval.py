from __future__ import annotations

import argparse
import gc
import json
import os
import tempfile
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Any

from huggingface_hub import hf_hub_download

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from memorymaster.models import CitationInput  # noqa: E402
from memorymaster.service import MemoryService  # noqa: E402


DATA_PATH = ROOT / "benchmark" / "data" / "longmemeval_s_cleaned.json"
RETRIEVAL_OUTPUT = ROOT / "benchmark" / "longmemeval_s_retrieval.json"
FULL_OUTPUT = ROOT / "benchmark" / "longmemeval_s_full.json"
REPO_ID = "xiaowu0162/longmemeval-cleaned"
FILENAME = "longmemeval_s_cleaned.json"
BENCH_SCOPE = "benchmark:longmemeval-s"
OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
JUDGE_MODEL = "gpt-4o"


@dataclass(frozen=True)
class RetrievalResult:
    question_id: str
    question_type: str
    answer_session_ids: list[str]
    top_session_ids: list[str]
    reciprocal_rank: float
    recall_at_5: bool
    recall_at_10: bool
    top_contexts: list[str]


def ensure_dataset() -> Path:
    if DATA_PATH.exists():
        print(f"[download] using cached {DATA_PATH}")
        return DATA_PATH

    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    print(f"[download] fetching {REPO_ID}/{FILENAME} -> {DATA_PATH}")
    downloaded = hf_hub_download(
        repo_id=REPO_ID,
        filename=FILENAME,
        repo_type="dataset",
        local_dir=str(DATA_PATH.parent),
    )
    print(f"[download] ready {downloaded}")
    return Path(downloaded)


def load_dataset() -> list[dict[str, Any]]:
    path = ensure_dataset()
    return json.loads(path.read_text(encoding="utf-8"))


def session_to_text(session: list[dict[str, Any]], session_id: str, session_date: str) -> str:
    parts = [f"Session ID: {session_id}", f"Session date: {session_date}"]
    for turn in session:
        role = str(turn.get("role") or "unknown")
        content = str(turn.get("content") or "").strip()
        if content:
            parts.append(f"{role}: {content}")
    return "\n".join(parts)


def init_ephemeral_service(tmpdir: Path) -> MemoryService:
    db_path = tmpdir / "memorymaster-longmemeval.db"
    old_qdrant = os.environ.pop("QDRANT_URL", None)
    try:
        service = MemoryService(db_target=db_path, workspace_root=tmpdir)
        service.init_db()
    finally:
        if old_qdrant is not None:
            os.environ["QDRANT_URL"] = old_qdrant
    return service


def ingest_haystack(service: MemoryService, item: dict[str, Any]) -> None:
    sessions = item["haystack_sessions"]
    session_ids = item["haystack_session_ids"]
    dates = item.get("haystack_dates") or [""] * len(sessions)
    qid = item["question_id"]

    for idx, session in enumerate(sessions):
        session_id = str(session_ids[idx])
        session_date = str(dates[idx]) if idx < len(dates) else ""
        text = session_to_text(session, session_id, session_date)
        if not text.strip():
            continue
        service.ingest(
            text=text,
            citations=[
                CitationInput(
                    source=session_id,
                    locator=f"question_id={qid};session_index={idx}",
                    excerpt=text[:500],
                )
            ],
            idempotency_key=f"longmemeval:{qid}:{session_id}",
            claim_type="fact",
            subject=session_id,
            predicate="conversation_session",
            object_value=session_date,
            scope=BENCH_SCOPE,
            confidence=1.0,
            volatility="low",
            source_agent=session_id,
        )


def query_memory(service: MemoryService, question: str, top_k: int = 10) -> list[dict[str, Any]]:
    return service.query_rows(
        query_text=question,
        limit=top_k,
        include_candidates=True,
        retrieval_mode="hybrid",
        vector_hook=lambda _text, _claims: {},
        scope_allowlist=[BENCH_SCOPE],
        allow_sensitive=True,
    )


def extract_session_id(row: dict[str, Any]) -> str:
    claim = row["claim"]
    if claim.source_agent:
        return claim.source_agent
    if claim.citations:
        return claim.citations[0].source
    return claim.subject or ""


def score_retrieval(item: dict[str, Any], rows: list[dict[str, Any]]) -> RetrievalResult:
    top_session_ids = [extract_session_id(row) for row in rows]
    gold = [str(sid) for sid in item.get("answer_session_ids") or []]
    gold_set = set(gold)
    reciprocal_rank = 0.0
    for rank, session_id in enumerate(top_session_ids, start=1):
        if session_id in gold_set:
            reciprocal_rank = 1.0 / rank
            break

    top_contexts = []
    for row in rows[:5]:
        claim = row["claim"]
        top_contexts.append(
            "\n".join(
                [
                    f"session_id: {extract_session_id(row)}",
                    f"score: {row.get('score', 0):.6f}",
                    claim.text[:4000],
                ]
            )
        )

    return RetrievalResult(
        question_id=str(item["question_id"]),
        question_type=str(item.get("question_type") or "unknown"),
        answer_session_ids=gold,
        top_session_ids=top_session_ids,
        reciprocal_rank=reciprocal_rank,
        recall_at_5=bool(gold_set & set(top_session_ids[:5])),
        recall_at_10=bool(gold_set & set(top_session_ids[:10])),
        top_contexts=top_contexts,
    )


def aggregate_retrieval(results: list[RetrievalResult]) -> dict[str, Any]:
    total = len(results)
    if total == 0:
        return {"count": 0, "recall_at_5": 0.0, "recall_at_10": 0.0, "mrr": 0.0}
    return {
        "count": total,
        "recall_at_5": sum(r.recall_at_5 for r in results) / total,
        "recall_at_10": sum(r.recall_at_10 for r in results) / total,
        "mrr": sum(r.reciprocal_rank for r in results) / total,
    }


def run_retrieval(data: list[dict[str, Any]], limit: int | None = None) -> tuple[dict[str, Any], list[RetrievalResult]]:
    selected = data[:limit] if limit else data
    results: list[RetrievalResult] = []
    start = time.time()

    for idx, item in enumerate(selected, start=1):
        with tempfile.TemporaryDirectory(prefix="mm-longmemeval-", ignore_cleanup_errors=True) as tmp:
            service = init_ephemeral_service(Path(tmp))
            try:
                ingest_haystack(service, item)
                rows = query_memory(service, str(item["question"]), top_k=10)
                results.append(score_retrieval(item, rows))
            finally:
                del service
                gc.collect()

        if idx % 25 == 0 or idx == len(selected):
            elapsed = time.time() - start
            rate = idx / elapsed if elapsed else 0.0
            print(f"[retrieval] {idx}/{len(selected)} complete ({rate:.2f} q/s)")

    aggregate = aggregate_retrieval(results)
    payload = {
        "mode": "retrieval-only",
        "dataset": "LongMemEval-S cleaned",
        "questions": len(selected),
        "retrieval_path": "MemoryService.query_rows hybrid lexical ranker over claims with vector_hook disabled",
        "ingest_path": "one MemoryMaster claim per haystack session",
        "metrics": aggregate,
        "results": [r.__dict__ for r in results],
        "elapsed_seconds": round(time.time() - start, 3),
    }
    RETRIEVAL_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    RETRIEVAL_OUTPUT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print_scores("retrieval", aggregate)
    print(f"[retrieval] wrote {RETRIEVAL_OUTPUT}")
    return payload, results


def call_openai_chat(prompt: str, api_key: str, *, max_tokens: int, temperature: float = 0.0) -> str:
    payload = {
        "model": JUDGE_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    data = json.dumps(payload).encode("utf-8")
    last_error: Exception | None = None
    for attempt in range(6):
        req = urllib.request.Request(
            OPENAI_CHAT_URL,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                result = json.loads(response.read().decode("utf-8"))
            return str(result["choices"][0]["message"]["content"]).strip()
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code not in {429, 500, 502, 503, 504}:
                body = exc.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"OpenAI HTTP {exc.code}: {body[:500]}") from exc
            sleep_for = min(60, 2**attempt)
            print(f"[openai] retryable HTTP {exc.code}; sleeping {sleep_for}s")
            time.sleep(sleep_for)
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            sleep_for = min(60, 2**attempt)
            print(f"[openai] retryable network error; sleeping {sleep_for}s")
            time.sleep(sleep_for)
    raise RuntimeError(f"OpenAI call failed after retries: {last_error}") from last_error


def answer_question(question: str, contexts: list[str], api_key: str) -> str:
    prompt = "\n\n".join(
        [
            "Answer the question using only the retrieved conversation context.",
            "If the context does not contain the answer, say I don't know.",
            "Keep the answer concise.",
            "Retrieved context:",
            "\n\n---\n\n".join(contexts),
            f"Question: {question}",
        ]
    )
    return call_openai_chat(prompt, api_key, max_tokens=300, temperature=0.0)


def judge_answer(gold: str, hypothesis: str, api_key: str) -> str:
    prompt = (
        "Given the gold answer X and the hypothesis Y, is Y correct? Reply YES or NO.\n\n"
        f"X: {gold}\n\n"
        f"Y: {hypothesis}"
    )
    verdict = call_openai_chat(prompt, api_key, max_tokens=5, temperature=0.0).upper()
    return "YES" if verdict.startswith("YES") else "NO"


def run_full(data: list[dict[str, Any]], retrieval_results: list[RetrievalResult], limit: int | None = None) -> dict[str, Any]:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required for --full")

    selected = data[:limit] if limit else data
    by_id = {result.question_id: result for result in retrieval_results}
    details = []
    by_type: dict[str, Counter[str]] = defaultdict(Counter)
    started = time.time()

    for idx, item in enumerate(selected, start=1):
        qid = str(item["question_id"])
        retrieval = by_id[qid]
        hypothesis = answer_question(str(item["question"]), retrieval.top_contexts, api_key)
        verdict = judge_answer(str(item["answer"]), hypothesis, api_key)
        correct = verdict == "YES"
        qtype = str(item.get("question_type") or "unknown")
        by_type[qtype]["total"] += 1
        by_type[qtype]["correct"] += int(correct)
        details.append(
            {
                "question_id": qid,
                "question_type": qtype,
                "answer": item["answer"],
                "hypothesis": hypothesis,
                "verdict": verdict,
                "correct": correct,
                "top_session_ids": retrieval.top_session_ids,
                "answer_session_ids": retrieval.answer_session_ids,
            }
        )

        if idx % 10 == 0 or idx == len(selected):
            accuracy = sum(d["correct"] for d in details) / len(details)
            print(f"[full] {idx}/{len(selected)} complete accuracy={accuracy:.4f}")
        time.sleep(0.2)

    total = len(details)
    correct_total = sum(d["correct"] for d in details)
    breakdown = {
        qtype: {
            "count": counts["total"],
            "correct": counts["correct"],
            "accuracy": counts["correct"] / counts["total"] if counts["total"] else 0.0,
        }
        for qtype, counts in sorted(by_type.items())
    }
    payload = {
        "mode": "full",
        "judge_model": JUDGE_MODEL,
        "questions": total,
        "accuracy": correct_total / total if total else 0.0,
        "correct": correct_total,
        "by_question_type": breakdown,
        "results": details,
        "elapsed_seconds": round(time.time() - started, 3),
    }
    FULL_OUTPUT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"[full] accuracy={payload['accuracy']:.4f} correct={correct_total}/{total}")
    print(f"[full] wrote {FULL_OUTPUT}")
    return payload


def print_scores(label: str, metrics: dict[str, Any]) -> None:
    print(
        f"[{label}] count={metrics['count']} "
        f"R@5={metrics['recall_at_5']:.4f} "
        f"R@10={metrics['recall_at_10']:.4f} "
        f"MRR={metrics['mrr']:.4f}"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MemoryMaster LongMemEval-S benchmark harness")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--retrieval-only", action="store_true", help="Run retrieval metrics only")
    mode.add_argument("--full", action="store_true", help="Run retrieval plus GPT-4o answer/judge pass")
    parser.add_argument("--limit", type=int, default=None, help="Optional question limit for partial runs")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    data = load_dataset()
    retrieval_payload, retrieval_results = run_retrieval(data, limit=args.limit)
    if args.full:
        full_payload = run_full(data, retrieval_results, limit=args.limit)
        print(f"RESULT_QA_ACCURACY={full_payload['accuracy']:.4f}")
    else:
        print("RESULT_QA_ACCURACY=deferred")
    metrics = retrieval_payload["metrics"]
    print(f"RESULT_R5={metrics['recall_at_5']:.4f}")
    print(f"RESULT_R10={metrics['recall_at_10']:.4f}")
    print(f"RESULT_MRR={metrics['mrr']:.4f}")


if __name__ == "__main__":
    main()

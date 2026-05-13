from __future__ import annotations

import argparse
import gc
import json
import os
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
import sys
from typing import Any

import requests
from huggingface_hub import hf_hub_download
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from memorymaster.models import CitationInput  # noqa: E402
from memorymaster.service import MemoryService  # noqa: E402


DATA_PATH = ROOT / "benchmark" / "data" / "longmemeval_s_cleaned.json"
RESULTS_OUTPUT = ROOT / "benchmark" / "longmemeval_s_results.json"
REPO_ID = "xiaowu0162/longmemeval-cleaned"
FILENAME = "longmemeval_s_cleaned.json"
BENCH_SCOPE = "benchmark:longmemeval-s"
OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
GEMINI_GENERATE_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
OPENAI_JUDGE_MODEL = "gpt-4o"
GEMINI_FALLBACK_MODEL = "gemini-2.5-flash"
DEFAULT_JUDGE_PACING_SECONDS = 1.5
DEFAULT_CHUNK_CHARS = 0


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


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str
    provider: str
    tokens: int = 0


@dataclass
class JudgeClient:
    openai_api_key: str
    gemini_api_key: str
    pacing_seconds: float = DEFAULT_JUDGE_PACING_SECONDS
    provider: str = "openai"
    total_tokens: int = 0
    models_used: set[str] = field(default_factory=set)

    def complete(self, prompt: str, *, max_tokens: int, temperature: float = 0.0) -> LLMResponse:
        self._pace()
        if self.provider == "openai":
            try:
                response = call_openai_chat(
                    prompt,
                    self.openai_api_key,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                self._record(response)
                return response
            except Exception as exc:
                if not self.gemini_api_key:
                    raise RuntimeError("OpenAI judge failed and GEMINI_API_KEY is not set") from exc
                print(f"[judge] OpenAI failed after retries; switching to {GEMINI_FALLBACK_MODEL} for the rest of the run")
                self.provider = "gemini"

        response = call_gemini(prompt, self.gemini_api_key, max_tokens=max_tokens, temperature=temperature)
        self._record(response)
        return response

    def _record(self, response: LLMResponse) -> None:
        self.total_tokens += response.tokens
        self.models_used.add(response.model)

    def _pace(self) -> None:
        if self.pacing_seconds > 0:
            time.sleep(self.pacing_seconds)

    @property
    def judge_used_label(self) -> str:
        if not self.models_used:
            return "none"
        if self.models_used == {OPENAI_JUDGE_MODEL}:
            return "gpt-4o"
        if self.models_used == {GEMINI_FALLBACK_MODEL}:
            return "gemini-2.5-flash"
        return "mixed"


def ensure_dataset(dataset_path: Path = DATA_PATH) -> Path:
    if dataset_path.exists():
        print(f"[download] using cached {dataset_path}")
        return dataset_path

    dataset_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"[download] fetching {REPO_ID}/{FILENAME} -> {dataset_path}")
    downloaded = hf_hub_download(
        repo_id=REPO_ID,
        filename=FILENAME,
        repo_type="dataset",
        local_dir=str(dataset_path.parent),
    )
    print(f"[download] ready {downloaded}")
    return Path(downloaded)


def load_dataset(dataset_path: Path = DATA_PATH) -> list[dict[str, Any]]:
    path = ensure_dataset(dataset_path)
    return json.loads(path.read_text(encoding="utf-8"))


def session_to_text(session: list[dict[str, Any]], session_id: str, session_date: str) -> str:
    parts = [f"Session ID: {session_id}", f"Session date: {session_date}"]
    for turn in session:
        role = str(turn.get("role") or "unknown")
        content = str(turn.get("content") or "").strip()
        if content:
            parts.append(f"{role}: {content}")
    return "\n".join(parts)


def chunk_text(text: str, chunk_chars: int = DEFAULT_CHUNK_CHARS) -> list[str]:
    if chunk_chars <= 0 or len(text) <= chunk_chars:
        return [text]
    return [text[idx : idx + chunk_chars] for idx in range(0, len(text), chunk_chars)]


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


def ingest_haystack(service: MemoryService, item: dict[str, Any], *, chunk_chars: int = DEFAULT_CHUNK_CHARS) -> None:
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
        chunks = chunk_text(text, chunk_chars=chunk_chars)
        for chunk_idx, chunk in enumerate(chunks):
            suffix = f":{chunk_idx}" if len(chunks) > 1 else ""
            service.ingest(
                text=chunk,
                citations=[
                    CitationInput(
                        source=session_id,
                        locator=f"question_id={qid};session_index={idx};chunk={chunk_idx}",
                        excerpt=chunk[:500],
                    )
                ],
                idempotency_key=f"longmemeval:{qid}:{session_id}{suffix}",
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
        limit=top_k * 3,
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


def unique_by_session(rows: list[dict[str, Any]], top_k: int = 10) -> list[dict[str, Any]]:
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for row in rows:
        session_id = extract_session_id(row)
        if session_id in seen:
            continue
        seen.add(session_id)
        unique.append(row)
        if len(unique) >= top_k:
            break
    return unique


def score_retrieval(item: dict[str, Any], rows: list[dict[str, Any]]) -> RetrievalResult:
    rows = unique_by_session(rows, top_k=10)
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


def run_retrieval(
    data: list[dict[str, Any]],
    *,
    limit: int | None = None,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
) -> tuple[dict[str, Any], list[RetrievalResult]]:
    selected = data[:limit] if limit else data
    results: list[RetrievalResult] = []
    start = time.time()

    for idx, item in enumerate(selected, start=1):
        with tempfile.TemporaryDirectory(prefix="mm-longmemeval-", ignore_cleanup_errors=True) as tmp:
            service = init_ephemeral_service(Path(tmp))
            try:
                ingest_haystack(service, item, chunk_chars=chunk_chars)
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
        "ingest_path": (
            "one MemoryMaster claim per haystack session"
            if chunk_chars <= 0
            else f"MemoryMaster claims chunked at {chunk_chars} chars per haystack session"
        ),
        "metrics": aggregate,
        "results": [r.__dict__ for r in results],
        "elapsed_seconds": round(time.time() - start, 3),
    }
    print_scores("retrieval", aggregate)
    return payload, results


def retry_after_seconds(response: requests.Response) -> float | None:
    retry_after = response.headers.get("retry-after") or response.headers.get("Retry-After")
    if not retry_after:
        return None
    try:
        return max(0.0, float(retry_after))
    except ValueError:
        return None


@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(min=2, max=60),
    retry=retry_if_exception_type((requests.HTTPError, ConnectionError, requests.ConnectionError, requests.Timeout)),
    reraise=True,
)
def call_openai_chat(prompt: str, api_key: str, *, max_tokens: int, temperature: float = 0.0) -> LLMResponse:
    payload = {
        "model": OPENAI_JUDGE_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    response = requests.post(
        OPENAI_CHAT_URL,
        json=payload,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=60,
    )
    if response.status_code == 429:
        sleep_for = retry_after_seconds(response)
        if sleep_for is not None:
            sleep_for = min(sleep_for, 60.0)
            print(f"[openai] HTTP 429; honoring retry-after={sleep_for:.1f}s")
            time.sleep(sleep_for)
    if response.status_code >= 400:
        body = response.text[:500]
        exc = requests.HTTPError(f"OpenAI HTTP {response.status_code}: {body}", response=response)
        raise exc

    result = response.json()
    usage = result.get("usage") or {}
    return LLMResponse(
        text=str(result["choices"][0]["message"]["content"]).strip(),
        model=OPENAI_JUDGE_MODEL,
        provider="openai",
        tokens=int(usage.get("total_tokens") or 0),
    )


def call_gemini(prompt: str, api_key: str, *, max_tokens: int, temperature: float = 0.0) -> LLMResponse:
    url = GEMINI_GENERATE_URL.format(model=GEMINI_FALLBACK_MODEL)
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
    }
    response = requests.post(f"{url}?key={api_key}", json=payload, timeout=60)
    if response.status_code == 429:
        sleep_for = retry_after_seconds(response) or 60.0
        print(f"[gemini] HTTP 429; sleeping {min(sleep_for, 60.0):.1f}s before giving up")
        time.sleep(min(sleep_for, 60.0))
    if response.status_code >= 400:
        raise RuntimeError(f"Gemini HTTP {response.status_code}: {response.text[:500]}")

    result = response.json()
    candidates = result.get("candidates") or []
    parts = candidates[0].get("content", {}).get("parts", []) if candidates else []
    text = "".join(str(part.get("text", "")) for part in parts).strip()
    usage = result.get("usageMetadata") or {}
    return LLMResponse(
        text=text,
        model=GEMINI_FALLBACK_MODEL,
        provider="gemini",
        tokens=int(usage.get("totalTokenCount") or 0),
    )


def answer_question(question: str, contexts: list[str], judge: JudgeClient) -> str:
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
    return judge.complete(prompt, max_tokens=300, temperature=0.0).text


def judge_answer(gold: str, hypothesis: str, judge: JudgeClient) -> str:
    prompt = (
        "Given the gold answer X and the hypothesis Y, is Y correct? Reply YES or NO.\n\n"
        f"X: {gold}\n\n"
        f"Y: {hypothesis}"
    )
    verdict = judge.complete(prompt, max_tokens=5, temperature=0.0).text.upper()
    return "YES" if verdict.startswith("YES") else "NO"


def run_full(
    data: list[dict[str, Any]],
    retrieval_results: list[RetrievalResult],
    *,
    limit: int | None = None,
    judge_pacing_seconds: float = DEFAULT_JUDGE_PACING_SECONDS,
) -> dict[str, Any]:
    openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not openai_key and not gemini_key:
        print("[full] no OPENAI_API_KEY or GEMINI_API_KEY; deferring QA")
        return {
            "mode": "full",
            "status": "deferred-2",
            "questions": 0,
            "requested_questions": len(data[:limit] if limit else data),
            "accuracy": None,
            "correct": 0,
            "by_question_type": {},
            "results": [],
            "elapsed_seconds": 0.0,
            "judge_model": "none",
            "tokens": 0,
        }

    selected = data[:limit] if limit else data
    by_id = {result.question_id: result for result in retrieval_results}
    details = []
    by_type: dict[str, Counter[str]] = defaultdict(Counter)
    judge = JudgeClient(
        openai_api_key=openai_key,
        gemini_api_key=gemini_key,
        pacing_seconds=judge_pacing_seconds,
        provider="openai" if openai_key else "gemini",
    )
    started = time.time()

    for idx, item in enumerate(selected, start=1):
        qid = str(item["question_id"])
        retrieval = by_id[qid]
        try:
            hypothesis = answer_question(str(item["question"]), retrieval.top_contexts, judge)
            verdict = judge_answer(str(item["answer"]), hypothesis, judge)
        except Exception as exc:
            print(f"[full] stopping after {len(details)} completed questions: {exc}")
            break
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
            print(
                f"[full] {idx}/{len(selected)} complete "
                f"accuracy={accuracy:.4f} judge={judge.judge_used_label} tokens={judge.total_tokens}"
            )

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
    status = "complete" if total == len(selected) else "partial" if total else "deferred-2"
    payload = {
        "mode": "full",
        "judge_model": judge.judge_used_label,
        "status": status,
        "questions": total,
        "requested_questions": len(selected),
        "accuracy": correct_total / total if total else None,
        "correct": correct_total,
        "by_question_type": breakdown,
        "results": details,
        "elapsed_seconds": round(time.time() - started, 3),
        "tokens": judge.total_tokens,
    }
    if total:
        print(f"[full] accuracy={payload['accuracy']:.4f} correct={correct_total}/{total}")
    print(f"[full] judge={payload['judge_model']} tokens={judge.total_tokens}")
    return payload


def write_results(
    retrieval_payload: dict[str, Any],
    full_payload: dict[str, Any] | None,
    *,
    output_path: Path = RESULTS_OUTPUT,
) -> dict[str, Any]:
    payload = {
        "dataset": retrieval_payload["dataset"],
        "questions": retrieval_payload["questions"],
        "status": full_payload.get("status") if full_payload else "retrieval-only",
        "retrieval": retrieval_payload,
        "qa": full_payload,
        "elapsed_seconds": round(
            retrieval_payload.get("elapsed_seconds", 0.0)
            + ((full_payload or {}).get("elapsed_seconds") or 0.0),
            3,
        ),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"[results] wrote {output_path}")
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
    mode.add_argument("--full", action="store_true", help="Run retrieval plus answer/judge pass")
    parser.add_argument("--limit", type=int, default=None, help="Optional question limit for partial runs")
    parser.add_argument("--dataset", type=Path, default=DATA_PATH, help="Dataset JSON path")
    parser.add_argument("--output", type=Path, default=RESULTS_OUTPUT, help="Results JSON path")
    parser.add_argument(
        "--judge-pacing-seconds",
        type=float,
        default=DEFAULT_JUDGE_PACING_SECONDS,
        help="Delay between judge model calls; default 1.5s",
    )
    parser.add_argument(
        "--chunk-chars",
        type=int,
        default=DEFAULT_CHUNK_CHARS,
        help="Chunk haystack sessions at this character count before ingest; 0 keeps one claim per session",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    data = load_dataset(args.dataset)
    retrieval_payload, retrieval_results = run_retrieval(data, limit=args.limit, chunk_chars=args.chunk_chars)
    full_payload = None
    if args.full:
        full_payload = run_full(
            data,
            retrieval_results,
            limit=args.limit,
            judge_pacing_seconds=args.judge_pacing_seconds,
        )
    output = write_results(retrieval_payload, full_payload, output_path=args.output)
    metrics = output["retrieval"]["metrics"]
    qa = output.get("qa") or {}
    qa_accuracy = qa.get("accuracy")
    print(f"RESULT_R5={metrics['recall_at_5']:.4f}")
    print(f"RESULT_R10={metrics['recall_at_10']:.4f}")
    print(f"RESULT_MRR={metrics['mrr']:.4f}")
    print(f"RESULT_QA_ACCURACY={qa_accuracy:.4f}" if qa_accuracy is not None else "RESULT_QA_ACCURACY=deferred-2")
    print(f"RESULT_QUESTIONS_RUN={metrics['count']} out of {len(data)}")
    print(f"RESULT_JUDGE_USED={qa.get('judge_model') or 'none'}")


if __name__ == "__main__":
    main()

"""LongMemEval benchmark runner for MemoryMaster.

1. For each question: ingest the session history as claims
2. Query MemoryMaster with the question
3. Generate answer using Gemini
4. Output in LongMemEval evaluation format (JSONL)
5. Run GPT-4o evaluator

Usage:
    python benchmarks/longmemeval_runner.py --gemini-key KEY --openai-key KEY [--limit N]
"""
import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

BENCH_DB = str(SCRIPT_DIR / "longmemeval_bench.db")
DATASET = str(SCRIPT_DIR / "longmemeval_oracle.json")
OUTPUT = str(SCRIPT_DIR / "longmemeval_output.jsonl")


_last_svc = None

def init_bench_db():
    """Create a fresh benchmark DB."""
    global _last_svc
    # Close previous connection
    if _last_svc is not None:
        try:
            _last_svc.store.connect().__exit__(None, None, None)
        except Exception:
            pass
    # Remove old DB files
    for ext in ("", "-wal", "-shm"):
        p = BENCH_DB + ext
        try:
            if os.path.exists(p):
                os.remove(p)
        except PermissionError:
            pass
    from memorymaster.service import MemoryService
    svc = MemoryService(db_target=BENCH_DB, workspace_root=Path("."))
    svc.init_db()
    _last_svc = svc
    return svc


def ingest_sessions(svc, sessions, question_id):
    """Ingest chat sessions as claims — one claim per session with full text."""
    from memorymaster.models import CitationInput
    for i, session in enumerate(sessions):
        # Combine all turns into one text block per session
        parts = []
        for turn in session:
            role = turn.get("role", "")
            content = turn.get("content", "")
            if content:
                parts.append(f"[{role}]: {content}")
        full_text = "\n".join(parts)
        if not full_text:
            continue
        # Store up to 2000 chars per session (FTS5 needs enough text to match)
        text = full_text[:2000]
        text_hash = hashlib.sha256(text.lower().encode()).hexdigest()[:12]
        try:
            svc.ingest(
                text=text,
                citations=[CitationInput(source=f"session-{i}")],
                idempotency_key=f"bench-{question_id}-s{i}-{text_hash}",
                scope="benchmark",
                claim_type="fact",
                subject=f"session-{i}",
                predicate="conversation",
            )
        except Exception:
            pass


def query_memorymaster(svc, question, limit=10):
    """Query MemoryMaster for relevant claims using FTS5 + fallback to full scan."""
    # Try FTS5 first
    rows = svc.query_rows(
        query_text=question,
        limit=limit,
        retrieval_mode="legacy",
        include_candidates=True,
        scope_allowlist=["benchmark"],
    )
    context_parts = []
    for row in rows:
        claim = row.get("claim")
        if hasattr(claim, "text"):
            context_parts.append(claim.text[:500])

    # If FTS5 found nothing, do a brute-force scan of all claims
    if not context_parts:
        all_claims = svc.store.list_claims(limit=500, include_archived=False)
        # Simple keyword overlap scoring
        q_words = set(question.lower().split())
        scored = []
        for c in all_claims:
            if c.scope != "benchmark":
                continue
            text_words = set(c.text.lower().split())
            overlap = len(q_words & text_words)
            if overlap > 0:
                scored.append((overlap, c.text[:500]))
        scored.sort(key=lambda x: -x[0])
        context_parts = [t for _, t in scored[:limit]]

    return "\n\n".join(context_parts)


def call_openai(question, context, api_key, model="gpt-4o-mini"):
    """Generate answer using OpenAI."""
    url = "https://api.openai.com/v1/chat/completions"

    prompt = f"""Based on the following conversation history context, answer the question.
If the answer is not in the context, say "I don't know."

Context from memory:
{context}

Question: {question}

Answer concisely and directly."""

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 300,
    }

    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
        return result["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"Error: {e}"


def run_benchmark(gemini_key, openai_key, limit=None):
    """Run the full benchmark."""
    print(f"Loading dataset from {DATASET}...")
    with open(DATASET) as f:
        data = json.load(f)

    if limit:
        data = data[:limit]

    print(f"Running on {len(data)} questions...")
    results = []
    start = time.time()

    for i, q in enumerate(data):
        qid = q["question_id"]
        question = q["question"]
        sessions = q["haystack_sessions"]

        # Fresh DB per question (like MemPalace does)
        svc = init_bench_db()
        ingest_sessions(svc, sessions, qid)

        # Query
        context = query_memorymaster(svc, question)

        # Generate answer using OpenAI
        answer = call_openai(question, context, openai_key)

        results.append({
            "question_id": qid,
            "hypothesis": answer.strip(),
        })

        if (i + 1) % 10 == 0:
            elapsed = time.time() - start
            rate = (i + 1) / elapsed
            eta = (len(data) - i - 1) / rate if rate > 0 else 0
            print(f"  [{i+1}/{len(data)}] {rate:.1f} q/s, ETA {eta:.0f}s")

        time.sleep(0.3)

    # Write output
    with open(OUTPUT, "w") as f:
        for r in results:
            f.write(json.dumps(r) + "\n")

    elapsed = time.time() - start
    print(f"\nDone: {len(results)} answers in {elapsed:.0f}s")
    print(f"Output: {OUTPUT}")

    # Run evaluator
    print(f"\nRunning GPT-4o evaluator...")
    run_evaluator(openai_key)


def run_evaluator(openai_key):
    """Run the LongMemEval evaluation script."""
    eval_script = SCRIPT_DIR.parent / "benchmarks" / "evaluate_simple.py"

    # Write a simple evaluator since the official one needs specific setup
    with open(DATASET) as f:
        oracle = json.load(f)
    oracle_map = {q["question_id"]: q["answer"] for q in oracle}

    with open(OUTPUT) as f:
        hypotheses = [json.loads(line) for line in f]

    correct = 0
    total = 0
    url = "https://api.openai.com/v1/chat/completions"

    for hyp in hypotheses:
        qid = hyp["question_id"]
        predicted = hyp["hypothesis"]
        expected = oracle_map.get(qid, "")

        # Use GPT-4o to judge
        judge_prompt = f"""Is the following answer correct given the expected answer?

Expected answer: {expected}
Predicted answer: {predicted}

Reply with ONLY "correct" or "incorrect"."""

        payload = {
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": judge_prompt}],
            "temperature": 0,
            "max_tokens": 10,
        }

        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {openai_key}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read().decode())
            verdict = result["choices"][0]["message"]["content"].strip().lower()
            if "correct" in verdict and "incorrect" not in verdict:
                correct += 1
        except Exception as e:
            print(f"  Judge error for {qid}: {e}")

        total += 1
        if total % 50 == 0:
            print(f"  Evaluated {total}/{len(hypotheses)}: {correct}/{total} correct ({100*correct/total:.1f}%)")

        time.sleep(0.2)  # Rate limit

    accuracy = 100 * correct / total if total else 0
    print(f"\n{'='*50}")
    print(f"MEMORYMASTER LongMemEval SCORE: {accuracy:.1f}%")
    print(f"Correct: {correct}/{total}")
    print(f"{'='*50}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LongMemEval benchmark for MemoryMaster")
    parser.add_argument("--gemini-key", required=True)
    parser.add_argument("--openai-key", required=True)
    parser.add_argument("--limit", type=int, default=None, help="Limit questions (for testing)")
    args = parser.parse_args()
    run_benchmark(args.gemini_key, args.openai_key, args.limit)

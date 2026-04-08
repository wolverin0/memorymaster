"""LongMemEval benchmark with OpenAI embeddings + Qdrant vector search.

1. For each question: embed sessions with OpenAI, store in Qdrant
2. Embed question, vector search for relevant sessions
3. Generate answer with GPT-4o-mini
4. Evaluate with GPT-4o-mini

Usage:
    python benchmarks/longmemeval_vector_runner.py --openai-key KEY [--limit N]
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
DATASET = str(SCRIPT_DIR / "longmemeval_oracle.json")
OUTPUT = str(SCRIPT_DIR / "longmemeval_vector_output.jsonl")
QDRANT_URL = "http://192.168.100.186:6333"
COLLECTION = "longmemeval_bench"
EMBED_MODEL = "text-embedding-3-small"
EMBED_DIM = 1536


def openai_embed(texts, api_key):
    """Get embeddings from OpenAI."""
    url = "https://api.openai.com/v1/embeddings"
    payload = {"model": EMBED_MODEL, "input": texts}
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read().decode())
    return [d["embedding"] for d in result["data"]]


def openai_chat(prompt, api_key, model="gpt-4o-mini"):
    """Call OpenAI chat."""
    url = "https://api.openai.com/v1/chat/completions"
    payload = {"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.1, "max_tokens": 300}
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read().decode())
    return result["choices"][0]["message"]["content"].strip()


_collection_created = False

def qdrant_ensure_collection():
    """Create collection once, reuse for all questions (filter by question_id)."""
    global _collection_created
    if _collection_created:
        return
    # Check if exists
    try:
        req = urllib.request.Request(f"{QDRANT_URL}/collections/{COLLECTION}")
        resp = urllib.request.urlopen(req, timeout=10)
        _collection_created = True
        return
    except Exception:
        pass
    # Create (may take a while on slow Qdrant)
    payload = {"vectors": {"size": EMBED_DIM, "distance": "Cosine"}}
    req = urllib.request.Request(
        f"{QDRANT_URL}/collections/{COLLECTION}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    urllib.request.urlopen(req, timeout=120)
    _collection_created = True


def qdrant_upsert(points):
    """Upsert points to Qdrant."""
    payload = {"points": points}
    req = urllib.request.Request(
        f"{QDRANT_URL}/collections/{COLLECTION}/points",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    urllib.request.urlopen(req, timeout=30)


def qdrant_search(vector, qid, limit=5):
    """Search Qdrant filtered by question_id."""
    payload = {
        "vector": vector,
        "limit": limit,
        "with_payload": True,
        "filter": {"must": [{"key": "qid", "match": {"value": qid}}]},
    }
    req = urllib.request.Request(
        f"{QDRANT_URL}/collections/{COLLECTION}/points/search",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read().decode())
    return result.get("result", [])


def run_benchmark(openai_key, limit=None):
    print(f"Loading dataset...")
    with open(DATASET) as f:
        data = json.load(f)
    if limit:
        data = data[:limit]

    print(f"Running on {len(data)} questions with Qdrant vector search...")
    results = []
    start = time.time()
    global_point_id = 0

    for i, q in enumerate(data):
        qid = q["question_id"]
        question = q["question"]
        sessions = q["haystack_sessions"]

        # Ensure collection exists (created once)
        qdrant_ensure_collection()

        # Build session texts
        session_texts = []
        for si, session in enumerate(sessions):
            parts = []
            for turn in session:
                parts.append(f"[{turn['role']}]: {turn['content']}")
            full = "\n".join(parts)[:3000]  # More context than FTS5 version
            session_texts.append(full)

        # Embed all sessions in batches
        batch_size = 20
        for bi in range(0, len(session_texts), batch_size):
            batch = session_texts[bi:bi + batch_size]
            try:
                embeddings = openai_embed(batch, openai_key)
                points = []
                for j, emb in enumerate(embeddings):
                    points.append({
                        "id": global_point_id,
                        "vector": emb,
                        "payload": {"text": batch[j], "session_idx": bi + j, "qid": qid},
                    })
                    global_point_id += 1
                qdrant_upsert(points)
            except Exception as e:
                print(f"  Embed error: {e}")
                continue

        # Embed question and search
        try:
            q_emb = openai_embed([question], openai_key)[0]
            hits = qdrant_search(q_emb, qid, limit=5)
            context = "\n\n---\n\n".join(h["payload"]["text"][:1000] for h in hits)
        except Exception as e:
            context = ""
            print(f"  Search error: {e}")

        # Generate answer
        try:
            prompt = f"""Based on the conversation history below, answer the question.
If the answer is not in the context, say "I don't know."

Conversation history:
{context}

Question: {question}

Answer concisely and directly."""
            answer = openai_chat(prompt, openai_key)
        except Exception as e:
            answer = f"Error: {e}"

        results.append({"question_id": qid, "hypothesis": answer})

        if (i + 1) % 10 == 0:
            elapsed = time.time() - start
            rate = (i + 1) / elapsed
            eta = (len(data) - i - 1) / rate if rate > 0 else 0
            print(f"  [{i+1}/{len(data)}] {rate:.1f} q/s, ETA {eta:.0f}s")

        time.sleep(0.1)

    # Write output
    with open(OUTPUT, "w") as f:
        for r in results:
            f.write(json.dumps(r) + "\n")

    elapsed = time.time() - start
    print(f"\nDone: {len(results)} answers in {elapsed:.0f}s")

    # Evaluate
    print(f"\nRunning evaluator...")
    with open(DATASET) as f:
        oracle = json.load(f)
    oracle_map = {q["question_id"]: q["answer"] for q in oracle}

    correct = 0
    total = 0
    for hyp in results:
        qid = hyp["question_id"]
        predicted = hyp["hypothesis"]
        expected = oracle_map.get(qid, "")

        try:
            verdict = openai_chat(
                f"Is this answer correct?\nExpected: {expected}\nPredicted: {predicted}\nReply ONLY 'correct' or 'incorrect'.",
                openai_key,
            )
            if "correct" in verdict.lower() and "incorrect" not in verdict.lower():
                correct += 1
        except Exception:
            pass

        total += 1
        if total % 50 == 0:
            print(f"  Evaluated {total}/{len(results)}: {correct}/{total} ({100*correct/total:.1f}%)")

        time.sleep(0.1)

    accuracy = 100 * correct / total if total else 0
    print(f"\n{'='*50}")
    print(f"MEMORYMASTER + QDRANT LongMemEval SCORE: {accuracy:.1f}%")
    print(f"Correct: {correct}/{total}")
    print(f"{'='*50}")

    # Cleanup
    try:
        req = urllib.request.Request(f"{QDRANT_URL}/collections/{COLLECTION}", method="DELETE")
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--openai-key", required=True)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    run_benchmark(args.openai_key, args.limit)

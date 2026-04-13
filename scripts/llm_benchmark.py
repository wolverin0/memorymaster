"""A/B benchmark: Gemini 2.5 Flash Lite vs Gemma 4 e4b (local, with thinking).

Replicates the auto-ingest hook curator prompt on a sample of real
Claude Code session transcripts. Captures latency, token counts, and
raw outputs for offline scoring.

Usage:
    python scripts/llm_benchmark.py \
      --transcripts <file1.jsonl> <file2.jsonl> ... \
      --out bench-out

Env required:
    GEMINI_API_KEY  (for arm A)
    OLLAMA_URL      (default http://localhost:11434, for arm B)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

CURATOR_PROMPT = """You are a memory curator. Extract max 3 non-obvious learnings.
Return JSON array: [{"text": "one-line", "claim_type": "fact|decision|constraint", "subject": "entity", "predicate": "aspect"}]
Only: bug root causes, decisions, gotchas, constraints. Never: credentials, IPs, paths, code. Empty array if nothing worth remembering."""

GEMINI_MODEL = "gemini-2.5-flash-lite"
OLLAMA_MODEL = "gemma4:e4b"
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")


def extract_assistant_text(transcript_path: Path, max_chars: int = 3000) -> str:
    """Mirror the auto-ingest hook's extraction logic, robust to both schemas."""
    messages: list[str] = []
    if not transcript_path.exists():
        return ""
    lines = transcript_path.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in reversed(lines[-200:]):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = entry.get("message") if isinstance(entry.get("message"), dict) else entry
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", "")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = " ".join(
                c.get("text", "")
                for c in content
                if isinstance(c, dict) and c.get("type") == "text"
            )
        text = text.strip()
        if text and len(text) > 30:
            messages.append(text[:500])
            if sum(len(m) for m in messages) > max_chars:
                break
    return "\n---\n".join(reversed(messages))


def _post_json(url: str, payload: dict, timeout: int = 600) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def call_gemini_flash_lite(system_prompt: str, user_text: str) -> dict:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return {"error": "GEMINI_API_KEY not set"}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={api_key}"
    payload = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"parts": [{"text": user_text}]}],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
        },
    }
    t0 = time.monotonic()
    try:
        data = _post_json(url, payload, timeout=120)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return {"error": f"HTTP {e.code}: {body[:300]}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    elapsed = time.monotonic() - t0
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as e:
        return {"error": f"unexpected shape: {e}", "raw": data}
    usage = data.get("usageMetadata", {})
    return {
        "text": text,
        "elapsed_s": round(elapsed, 3),
        "input_tokens": usage.get("promptTokenCount", 0),
        "output_tokens": usage.get("candidatesTokenCount", 0),
        "thinking_tokens": usage.get("thoughtsTokenCount", 0),
    }


def call_gemma_thinking(system_prompt: str, user_text: str) -> dict:
    """Call Gemma 4 e4b via Ollama with <|think|> token in system."""
    full_system = f"<|think|>\n{system_prompt}"
    payload = {
        "model": OLLAMA_MODEL,
        "system": full_system,
        "prompt": user_text,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.2, "num_ctx": 8192, "num_predict": 1024},
    }
    t0 = time.monotonic()
    try:
        data = _post_json(f"{OLLAMA_URL}/api/generate", payload, timeout=600)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return {"error": f"HTTP {e.code}: {body[:300]}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    elapsed = time.monotonic() - t0
    return {
        "text": data.get("response", ""),
        "elapsed_s": round(elapsed, 3),
        "input_tokens": data.get("prompt_eval_count", 0),
        "output_tokens": data.get("eval_count", 0),
        "thinking_tokens": 0,  # Ollama doesn't separate
        "load_duration_ms": round(data.get("load_duration", 0) / 1e6, 1),
    }


def parse_json_array(text: str) -> list:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        v = json.loads(text)
        return v if isinstance(v, list) else [v]
    except json.JSONDecodeError:
        i, j = text.find("["), text.rfind("]")
        if i >= 0 and j > i:
            try:
                v = json.loads(text[i : j + 1])
                return v if isinstance(v, list) else [v]
            except json.JSONDecodeError:
                pass
    return []


def short_id(path: Path) -> str:
    parts = path.parent.name.split("-")
    proj = parts[-1] if parts else path.parent.name
    return f"{proj[:24]}_{path.stem[:8]}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--transcripts", nargs="+", required=True, help="JSONL transcript paths")
    ap.add_argument("--out", default="bench-out", help="Output dir")
    ap.add_argument("--max-input-chars", type=int, default=3000)
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = []
    print(f"=== Benchmark: {len(args.transcripts)} transcripts ===")
    print(f"Arm A: Gemini {GEMINI_MODEL}")
    print(f"Arm B: Ollama {OLLAMA_MODEL} (thinking via <|think|> system token)")
    print()

    for i, t in enumerate(args.transcripts, 1):
        tp = Path(t)
        sid = short_id(tp)
        case_dir = out_dir / sid
        case_dir.mkdir(exist_ok=True)

        text = extract_assistant_text(tp, args.max_input_chars)
        if not text or len(text) < 100:
            print(f"[{i:2d}/{len(args.transcripts)}] {sid}: SKIPPED (input too small: {len(text)} chars)")
            continue
        (case_dir / "input.txt").write_text(text, encoding="utf-8")

        print(f"[{i:2d}/{len(args.transcripts)}] {sid}: input={len(text)}c", end="", flush=True)

        # Arm A
        a = call_gemini_flash_lite(CURATOR_PROMPT, text)
        a["claims"] = parse_json_array(a.get("text", "")) if "text" in a else []
        (case_dir / "A_flashlite.json").write_text(json.dumps(a, indent=2, ensure_ascii=False), encoding="utf-8")
        if "error" in a:
            print(f" | A=ERR({a['error'][:50]})", end="", flush=True)
        else:
            print(f" | A={len(a['claims'])}c {a['elapsed_s']}s", end="", flush=True)

        # Arm B
        b = call_gemma_thinking(CURATOR_PROMPT, text)
        b["claims"] = parse_json_array(b.get("text", "")) if "text" in b else []
        (case_dir / "B_gemma_thinking.json").write_text(json.dumps(b, indent=2, ensure_ascii=False), encoding="utf-8")
        if "error" in b:
            print(f" | B=ERR({b['error'][:50]})")
        else:
            print(f" | B={len(b['claims'])}c {b['elapsed_s']}s")

        rows.append({
            "case": sid,
            "input_chars": len(text),
            "A_elapsed_s": a.get("elapsed_s"),
            "A_in_tok": a.get("input_tokens"),
            "A_out_tok": a.get("output_tokens"),
            "A_think_tok": a.get("thinking_tokens"),
            "A_claims": len(a.get("claims", [])),
            "A_err": a.get("error", ""),
            "B_elapsed_s": b.get("elapsed_s"),
            "B_in_tok": b.get("input_tokens"),
            "B_out_tok": b.get("output_tokens"),
            "B_claims": len(b.get("claims", [])),
            "B_load_ms": b.get("load_duration_ms"),
            "B_err": b.get("error", ""),
        })

    # Write metrics CSV
    if rows:
        csv_path = out_dir / "metrics.csv"
        with csv_path.open("w", encoding="utf-8") as f:
            keys = list(rows[0].keys())
            f.write(",".join(keys) + "\n")
            for r in rows:
                f.write(",".join(str(r.get(k, "")) for k in keys) + "\n")

        # Aggregates
        a_lat = [r["A_elapsed_s"] for r in rows if r["A_elapsed_s"]]
        b_lat = [r["B_elapsed_s"] for r in rows if r["B_elapsed_s"]]
        a_claims = sum(r["A_claims"] for r in rows)
        b_claims = sum(r["B_claims"] for r in rows)
        a_errs = sum(1 for r in rows if r["A_err"])
        b_errs = sum(1 for r in rows if r["B_err"])

        print()
        print("=== AGGREGATE ===")
        print(f"Cases run:        {len(rows)}")
        print(f"A (Flash Lite):   total_claims={a_claims}  errors={a_errs}  median_lat={sorted(a_lat)[len(a_lat)//2] if a_lat else '?'}s")
        print(f"B (Gemma e4b):    total_claims={b_claims}  errors={b_errs}  median_lat={sorted(b_lat)[len(b_lat)//2] if b_lat else '?'}s")
        print(f"Outputs in: {out_dir.resolve()}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

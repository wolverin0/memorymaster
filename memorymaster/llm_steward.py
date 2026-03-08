"""LLM Steward — automated claim extraction and curation.

Supports multiple LLM providers:
  - gemini (default, free tier available)
  - openai (GPT-4o, GPT-4o-mini, etc.)
  - anthropic (Claude Sonnet, Haiku, etc.)
  - Any OpenAI-compatible API (Ollama, Together, Groq, etc.)
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
import urllib.request
import urllib.error
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Provider configurations
PROVIDERS = {
    "gemini": {
        "url": "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}",
        "default_model": "gemini-2.5-flash",
        "format": "gemini",
    },
    "openai": {
        "url": "https://api.openai.com/v1/chat/completions",
        "default_model": "gpt-4o-mini",
        "format": "openai",
    },
    "anthropic": {
        "url": "https://api.anthropic.com/v1/messages",
        "default_model": "claude-haiku-4-5-20251001",
        "format": "anthropic",
    },
    "ollama": {
        "url": "http://localhost:11434/v1/chat/completions",
        "default_model": "llama3.2",
        "format": "openai",
    },
    "custom": {
        "url": "",  # User must provide --base-url
        "default_model": "",
        "format": "openai",  # Assume OpenAI-compatible
    },
}

EXTRACT_PROMPT = """You are a memory curator for an AI coding agent system.
Given a raw memory text, extract structured knowledge claims.

For each distinct fact/decision/learning in the text, output a JSON object with:
- "subject": the entity (e.g. "ClawTrol API", "memorymaster", "SSH access")
- "predicate": the relationship (e.g. "runs_on", "requires", "is_located_at", "learned")
- "object_value": the specific value (e.g. "port 4001", "Python 3.10+", "192.168.100.186")
- "confidence": 0.0-1.0 how certain this fact is (1.0 = explicit statement, 0.5 = inferred, 0.2 = speculative)
- "action": "confirm" if this is a solid fact, "archive" if it's noise/chatter/duplicate/not useful

Rules:
- Extract ANY technical fact: config values, IPs, ports, commands, file paths, architecture decisions, bug fixes, lessons learned, workflows, API endpoints, tool behavior
- Also extract decisions ("we decided to use X"), status facts ("X is working/broken"), and process knowledge ("to do X you need Y")
- The text may be in any language — extract facts in English
- Skip ONLY pure greetings, filler ("ok", "let me check"), or content with zero factual information
- One text may contain 1-5 claims. Prefer extracting MORE claims over fewer.
- ONLY return empty array [] if the text truly has no factual content at all
- Output ONLY a JSON array, no markdown, no explanation

Text to analyze:
---
{text}
---

Output JSON array:"""


@dataclass
class ExtractionResult:
    claim_id: int
    original_text: str
    extractions: list[dict]
    action: str  # "confirm", "archive", "skip"
    raw_response: str


def _build_gemini_payload(prompt: str) -> bytes:
    return json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 1024},
    }).encode("utf-8")


def _build_openai_payload(prompt: str, model: str) -> bytes:
    return json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 1024,
    }).encode("utf-8")


def _build_anthropic_payload(prompt: str, model: str) -> bytes:
    return json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 1024,
    }).encode("utf-8")


def _extract_gemini_text(data: dict) -> str:
    candidates = data.get("candidates", [])
    if candidates:
        parts = candidates[0].get("content", {}).get("parts", [])
        if parts:
            return parts[0].get("text", "")
    return ""


def _extract_openai_text(data: dict) -> str:
    choices = data.get("choices", [])
    if choices:
        return choices[0].get("message", {}).get("content", "")
    return ""


def _extract_anthropic_text(data: dict) -> str:
    content = data.get("content", [])
    if content:
        return content[0].get("text", "")
    return ""


def _call_llm(
    provider: str,
    api_key: str,
    model: str,
    prompt: str,
    base_url: str = "",
    max_retries: int = 2,
) -> str:
    """Call any supported LLM provider and return text response."""
    cfg = PROVIDERS.get(provider, PROVIDERS["custom"])
    fmt = cfg["format"]

    # Build URL
    if base_url:
        url = base_url.rstrip("/")
        if fmt == "openai" and "/chat/completions" not in url:
            url += "/chat/completions"
    else:
        url = cfg["url"].format(model=model, api_key=api_key)

    # Build payload and headers
    headers = {"Content-Type": "application/json"}

    if fmt == "gemini":
        payload = _build_gemini_payload(prompt)
    elif fmt == "anthropic":
        payload = _build_anthropic_payload(prompt, model)
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
    else:  # openai format
        payload = _build_openai_payload(prompt, model)
        if api_key and provider != "ollama":
            headers["Authorization"] = f"Bearer {api_key}"

    # Extract response text
    extractors = {
        "gemini": _extract_gemini_text,
        "openai": _extract_openai_text,
        "anthropic": _extract_anthropic_text,
    }
    extract_fn = extractors.get(fmt, _extract_openai_text)

    for attempt in range(max_retries + 1):
        try:
            req = urllib.request.Request(
                url, data=payload, headers=headers, method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return extract_fn(data)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < max_retries:
                wait = 4 * (2 ** attempt)
                log.info("Rate limited, waiting %ds...", wait)
                time.sleep(wait)
                continue
            raise
        except Exception:
            if attempt < max_retries:
                time.sleep(2)
                continue
            raise
    return ""


def _parse_extractions(raw: str) -> list[dict]:
    """Parse LLM response into list of extraction dicts."""
    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines)
    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            return [result]
    except json.JSONDecodeError:
        start = text.find("[")
        end = text.rfind("]")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass
    return []


def extract_claim(
    provider: str, api_key: str, model: str,
    claim_id: int, text: str,
    base_url: str = "",
) -> ExtractionResult:
    """Extract structured claims from raw text using any LLM."""
    prompt = EXTRACT_PROMPT.replace("{text}", text[:2000])
    raw = _call_llm(provider, api_key, model, prompt, base_url)
    extractions = _parse_extractions(raw)

    if not extractions:
        action = "archive"
    else:
        actions = [e.get("action", "confirm") for e in extractions]
        action = "archive" if all(a == "archive" for a in actions) else "confirm"

    return ExtractionResult(
        claim_id=claim_id,
        original_text=text,
        extractions=extractions,
        action=action,
        raw_response=raw,
    )


def run_steward(
    db_path: str,
    api_key: str,
    provider: str = "gemini",
    model: str = "",
    base_url: str = "",
    limit: int = 50,
    dry_run: bool = False,
    delay: float = 5.0,
) -> dict:
    """Process candidate claims through LLM extraction and curation.

    Returns summary stats dict.
    """
    cfg = PROVIDERS.get(provider, PROVIDERS["custom"])
    if not model:
        model = cfg["default_model"]

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    candidates = conn.execute(
        "SELECT id, text FROM claims WHERE status = 'candidate' "
        "AND text IS NOT NULL ORDER BY id LIMIT ?",
        (limit,),
    ).fetchall()

    stats = {
        "total": len(candidates),
        "confirmed": 0,
        "archived": 0,
        "errors": 0,
        "claims_extracted": 0,
        "provider": provider,
        "model": model,
        "results": [],
    }

    for row in candidates:
        claim_id = row["id"]
        text = row["text"] or ""

        if len(text.strip()) < 10:
            if not dry_run:
                conn.execute(
                    "UPDATE claims SET status = 'archived' WHERE id = ?",
                    (claim_id,),
                )
                conn.execute(
                    "INSERT INTO events (claim_id, event_type, details, created_at) "
                    "VALUES (?, 'transition', 'auto-archived: too short', datetime('now'))",
                    (claim_id,),
                )
            stats["archived"] += 1
            continue

        try:
            result = extract_claim(provider, api_key, model, claim_id, text, base_url)
        except Exception as e:
            log.warning("LLM error for claim #%d: %s", claim_id, e)
            stats["errors"] += 1
            stats["results"].append({"claim_id": claim_id, "error": str(e)})
            continue

        if result.action == "archive":
            if not dry_run:
                conn.execute(
                    "UPDATE claims SET status = 'archived' WHERE id = ?",
                    (claim_id,),
                )
                conn.execute(
                    "INSERT INTO events (claim_id, event_type, details, created_at) "
                    "VALUES (?, 'transition', 'llm-archived: no useful claims', datetime('now'))",
                    (claim_id,),
                )
            stats["archived"] += 1
        else:
            confirm_extractions = [
                e for e in result.extractions if e.get("action") != "archive"
            ]
            if confirm_extractions:
                first = confirm_extractions[0]
                f_subj = first.get("subject")
                f_pred = first.get("predicate")
                f_obj = first.get("object_value")
                f_conf = min(1.0, max(0.0, float(first.get("confidence", 0.7))))
                if not dry_run:
                    dup = conn.execute(
                        "SELECT id FROM claims WHERE subject = ? AND predicate = ? "
                        "AND status = 'confirmed' AND scope = 'project' AND id != ? LIMIT 1",
                        (f_subj, f_pred, claim_id),
                    ).fetchone()
                    if dup:
                        conn.execute(
                            "UPDATE claims SET status = 'archived' WHERE id = ?",
                            (claim_id,),
                        )
                        conn.execute(
                            "UPDATE claims SET object_value = ?, confidence = MAX(confidence, ?), "
                            "updated_at = datetime('now') WHERE id = ?",
                            (f_obj, f_conf, dup["id"]),
                        )
                    else:
                        conn.execute(
                            "UPDATE claims SET status = 'confirmed', "
                            "subject = ?, predicate = ?, object_value = ?, "
                            "confidence = ? WHERE id = ?",
                            (f_subj, f_pred, f_obj, f_conf, claim_id),
                        )
                    conn.execute(
                        "INSERT INTO events (claim_id, event_type, details, created_at) "
                        "VALUES (?, 'transition', ?, datetime('now'))",
                        (claim_id, f"llm-confirmed: {f_subj}/{f_pred}"),
                    )

                    for extra in confirm_extractions[1:]:
                        subj = extra.get("subject")
                        pred = extra.get("predicate")
                        obj_val = extra.get("object_value")
                        conf = min(1.0, max(0.0, float(extra.get("confidence", 0.7))))

                        existing = conn.execute(
                            "SELECT id FROM claims WHERE subject = ? AND predicate = ? "
                            "AND status = 'confirmed' AND scope = 'project' LIMIT 1",
                            (subj, pred),
                        ).fetchone()

                        if existing:
                            conn.execute(
                                "UPDATE claims SET object_value = ?, confidence = MAX(confidence, ?), "
                                "updated_at = datetime('now') WHERE id = ?",
                                (obj_val, conf, existing["id"]),
                            )
                        else:
                            try:
                                cursor = conn.execute(
                                    "INSERT INTO claims (text, subject, predicate, object_value, "
                                    "confidence, status, scope, created_at, updated_at) "
                                    "VALUES (?, ?, ?, ?, ?, 'confirmed', 'project', datetime('now'), datetime('now'))",
                                    (text[:200], subj, pred, obj_val, conf),
                                )
                                new_id = cursor.lastrowid
                                conn.execute(
                                    "INSERT INTO events (claim_id, event_type, details, created_at) "
                                    "VALUES (?, 'transition', ?, datetime('now'))",
                                    (new_id, f"llm-extracted from claim #{claim_id}"),
                                )
                                stats["claims_extracted"] += 1
                            except sqlite3.IntegrityError:
                                pass

                stats["confirmed"] += 1
            else:
                if not dry_run:
                    conn.execute(
                        "UPDATE claims SET status = 'archived' WHERE id = ?",
                        (claim_id,),
                    )
                stats["archived"] += 1

        stats["results"].append({
            "claim_id": claim_id,
            "action": result.action,
            "extractions": len(result.extractions),
            "preview": text[:80],
        })

        if not dry_run and (stats["confirmed"] + stats["archived"]) % 10 == 0:
            conn.commit()

        time.sleep(delay)

    if not dry_run:
        conn.commit()
    conn.close()

    return stats


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="LLM Steward for MemoryMaster — automated claim extraction and curation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Gemini (free)
  memorymaster-steward --db memory.db --provider gemini --api-key YOUR_KEY

  # OpenAI
  memorymaster-steward --db memory.db --provider openai --api-key sk-... --model gpt-4o-mini

  # Anthropic
  memorymaster-steward --db memory.db --provider anthropic --api-key sk-ant-... --model claude-haiku-4-5-20251001

  # Ollama (local, no key needed)
  memorymaster-steward --db memory.db --provider ollama --model llama3.2

  # Any OpenAI-compatible API
  memorymaster-steward --db memory.db --provider custom --base-url https://api.together.xyz/v1 --api-key KEY --model meta-llama/Llama-3-8b
""",
    )
    parser.add_argument("--db", required=True, help="Path to memorymaster SQLite DB")
    parser.add_argument("--api-key", default="", help="API key for the LLM provider")
    parser.add_argument(
        "--provider", default="gemini",
        choices=["gemini", "openai", "anthropic", "ollama", "custom"],
        help="LLM provider (default: gemini)",
    )
    parser.add_argument("--model", default="", help="Model name (uses provider default if omitted)")
    parser.add_argument("--base-url", default="", help="Custom API base URL (for custom/ollama providers)")
    parser.add_argument("--limit", type=int, default=50, help="Max claims to process (default: 50)")
    parser.add_argument("--delay", type=float, default=5.0, help="Seconds between API calls (default: 5.0)")
    parser.add_argument("--dry-run", action="store_true", help="Don't modify DB, just show what would happen")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    stats = run_steward(
        args.db, args.api_key, args.provider, args.model,
        args.base_url, args.limit, args.dry_run, args.delay,
    )

    print(f"\n{'='*50}")
    print(f"LLM Steward Results ({stats['provider']}/{stats['model']})")
    print(f"{'='*50}")
    print(f"Processed:  {stats['total']}")
    print(f"Confirmed:  {stats['confirmed']}")
    print(f"Archived:   {stats['archived']}")
    print(f"New claims: {stats['claims_extracted']}")
    print(f"Errors:     {stats['errors']}")
    print()
    for r in stats["results"]:
        if "error" in r:
            print(f"  #{r['claim_id']}: ERROR - {r['error']}")
        else:
            print(f"  #{r['claim_id']}: {r['action']} ({r['extractions']} extractions) — {r.get('preview','')}")


if __name__ == "__main__":
    main()

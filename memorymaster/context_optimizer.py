"""Context window optimizer — packs ranked claims into a token budget.

Greedy knapsack: highest-relevance claims first, skip if a claim exceeds
remaining budget.  Three output formats aimed at AI agent consumption:

- ``text``  — human-readable summary block
- ``xml``   — XML-tagged blocks for system prompts
- ``json``  — structured JSON array
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from memorymaster.models import Claim

OUTPUT_FORMATS = ("text", "xml", "json")

# Rough token estimate: 1 token ~ 4 chars (conservative for English text).
# Works across GPT / Claude / Llama tokenisers without requiring tiktoken.
_CHARS_PER_TOKEN = 4


@dataclass(slots=True)
class ContextResult:
    """Return value of the context optimizer."""

    output: str
    claims_considered: int
    claims_included: int
    tokens_used: int
    token_budget: int
    format: str


def estimate_tokens(text: str) -> int:
    """Estimate token count from character length."""
    return max(1, len(text) // _CHARS_PER_TOKEN)


def _claim_summary(claim: Claim) -> str:
    """Single-line summary used for token estimation and text output."""
    parts: list[str] = []
    if claim.subject:
        parts.append(claim.subject)
    if claim.predicate:
        parts.append(claim.predicate)
    if claim.object_value:
        parts.append(claim.object_value)
    triple = " | ".join(parts) if parts else ""
    status_tag = f"[{claim.status}]" if claim.status != "confirmed" else ""
    pin_tag = " [pinned]" if claim.pinned else ""
    prefix = " ".join(filter(None, [status_tag, pin_tag])).strip()
    if prefix:
        prefix = f" ({prefix})"
    if triple:
        return f"{claim.text}{prefix}  [{triple}]"
    return f"{claim.text}{prefix}"


def _claim_block_text(claim: Claim, score: float) -> str:
    """Full text block for one claim in ``text`` format."""
    summary = _claim_summary(claim)
    meta_parts = [
        f"id={claim.id}",
        f"score={score:.3f}",
        f"conf={claim.confidence:.2f}",
        f"scope={claim.scope}",
        f"vol={claim.volatility}",
    ]
    if claim.updated_at:
        meta_parts.append(f"updated={claim.updated_at}")
    meta = "  ".join(meta_parts)
    lines = [f"- {summary}", f"  ({meta})"]
    for citation in claim.citations:
        cite_parts = [citation.source]
        if citation.locator:
            cite_parts.append(citation.locator)
        lines.append(f"  cite: {' | '.join(cite_parts)}")
    return "\n".join(lines)


def _claim_xml(claim: Claim, score: float) -> str:
    """XML element for one claim."""
    attrs = (
        f'id="{claim.id}" score="{score:.3f}" status="{claim.status}" '
        f'confidence="{claim.confidence:.2f}" scope="{claim.scope}" '
        f'volatility="{claim.volatility}"'
    )
    if claim.pinned:
        attrs += ' pinned="true"'
    lines = [f"<claim {attrs}>"]
    lines.append(f"  <text>{_xml_escape(claim.text)}</text>")
    if claim.subject or claim.predicate or claim.object_value:
        lines.append(
            f"  <triple subject=\"{_xml_escape(claim.subject or '')}\" "
            f"predicate=\"{_xml_escape(claim.predicate or '')}\" "
            f"object=\"{_xml_escape(claim.object_value or '')}\" />"
        )
    if claim.citations:
        lines.append("  <citations>")
        for c in claim.citations:
            loc = f' locator="{_xml_escape(c.locator)}"' if c.locator else ""
            lines.append(f"    <cite source=\"{_xml_escape(c.source)}\"{loc} />")
        lines.append("  </citations>")
    lines.append("</claim>")
    return "\n".join(lines)


def _claim_json_entry(claim: Claim, score: float) -> dict[str, Any]:
    """Structured dict for one claim in ``json`` format."""
    entry: dict[str, Any] = {
        "id": claim.id,
        "text": claim.text,
        "score": round(score, 4),
        "status": claim.status,
        "confidence": round(claim.confidence, 3),
        "scope": claim.scope,
        "volatility": claim.volatility,
        "pinned": claim.pinned,
    }
    if claim.subject or claim.predicate or claim.object_value:
        entry["triple"] = {
            "subject": claim.subject,
            "predicate": claim.predicate,
            "object": claim.object_value,
        }
    if claim.citations:
        entry["citations"] = [
            {
                "source": c.source,
                **({"locator": c.locator} if c.locator else {}),
            }
            for c in claim.citations
        ]
    if claim.updated_at:
        entry["updated_at"] = claim.updated_at
    return entry


def _xml_escape(text: str | None) -> str:
    if not text:
        return ""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _get_format_overhead(output_format: str) -> tuple[str, str, int]:
    """Get header, footer_template, and overhead token count for the given format."""
    if output_format == "text":
        header = "# Relevant Memory Claims\n\n"
        footer_template = "\n---\n{included}/{considered} claims | {tokens}/{budget} tokens"
        overhead = estimate_tokens(header) + estimate_tokens(footer_template) + 10
    elif output_format == "xml":
        header = "<memory-context>\n"
        footer_template = "</memory-context>"
        overhead = estimate_tokens(header) + estimate_tokens(footer_template) + 5
    else:  # JSON
        header = ""
        footer_template = ""
        overhead = 30  # for {"claims":[], "meta":{...}}
    return header, footer_template, overhead


def pack_context(
    ranked_rows: list[dict[str, Any]],
    *,
    token_budget: int = 4000,
    output_format: str = "text",
) -> ContextResult:
    """Pack ranked claim rows into a token budget using greedy knapsack.

    Parameters
    ----------
    ranked_rows:
        Output of ``MemoryService.query_rows()`` — list of dicts with
        ``claim`` (Claim) and ``score`` (float) keys.
    token_budget:
        Maximum tokens for the output block.
    output_format:
        One of ``text``, ``xml``, ``json``.

    Returns
    -------
    ContextResult with the formatted output and metadata.
    """
    if output_format not in OUTPUT_FORMATS:
        raise ValueError(f"Unknown format '{output_format}'. Choose from: {', '.join(OUTPUT_FORMATS)}")
    if token_budget <= 0:
        raise ValueError("token_budget must be positive.")

    claims_considered = len(ranked_rows)

    # Reserve tokens for header/footer framing.
    header, footer_template, overhead = _get_format_overhead(output_format)

    available = max(1, token_budget - overhead)
    used = 0
    included: list[tuple[str, dict[str, Any]]] = []

    for row in ranked_rows:
        claim: Claim = row["claim"]
        score = float(row.get("score", 0.0))

        if output_format == "text":
            block = _claim_block_text(claim, score)
        elif output_format == "xml":
            block = _claim_xml(claim, score)
        else:
            block = json.dumps(_claim_json_entry(claim, score))

        block_tokens = estimate_tokens(block)
        if used + block_tokens > available:
            continue  # greedy knapsack: skip, try smaller claims
        included.append((block, row))
        used += block_tokens

    # Assemble final output based on format
    if output_format == "text":
        body = "(no claims fit within token budget)" if not included else "\n\n".join(block for block, _ in included)
        footer = footer_template.format(
            included=len(included),
            considered=claims_considered,
            tokens=used + overhead,
            budget=token_budget,
        )
        output = f"{header}{body}{footer}"
    elif output_format == "xml":
        inner = "  <!-- no claims fit within token budget -->\n" if not included else "\n".join(block for block, _ in included) + "\n"
        meta = f'<meta claims_included="{len(included)}" claims_considered="{claims_considered}" tokens_used="{used + overhead}" token_budget="{token_budget}" />\n'
        output = f"{header}{meta}{inner}{footer_template}"
    else:  # JSON
        entries = [_claim_json_entry(row["claim"], float(row.get("score", 0.0))) for _, row in included]
        output = json.dumps({
            "claims": entries,
            "meta": {
                "claims_included": len(included),
                "claims_considered": claims_considered,
                "tokens_used": used + overhead,
                "token_budget": token_budget,
            },
        }, indent=2)

    total_tokens = used + overhead
    return ContextResult(
        output=output,
        claims_considered=claims_considered,
        claims_included=len(included),
        tokens_used=total_tokens,
        token_budget=token_budget,
        format=output_format,
    )

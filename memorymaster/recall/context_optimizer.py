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

from memorymaster.core.models import Claim

OUTPUT_FORMATS = ("text", "xml", "json")
PROVIDERS = ("claude_cli", "google", "openai", "anthropic", "ollama", "auto")

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


@dataclass(frozen=True, slots=True)
class ProviderPackingProfile:
    """Provider-specific packing strategy."""

    provider: str
    chunk_tokens: int
    ordering: str


@dataclass(frozen=True, slots=True)
class _PackedBlock:
    block: str
    row: dict[str, Any]
    tokens: int
    score: float


_PROVIDER_PROFILES = {
    "claude_cli": ProviderPackingProfile("claude_cli", chunk_tokens=1600, ordering="stable_large"),
    "google": ProviderPackingProfile("google", chunk_tokens=300, ordering="score"),
    "openai": ProviderPackingProfile("openai", chunk_tokens=800, ordering="score_medium"),
    "anthropic": ProviderPackingProfile("anthropic", chunk_tokens=1600, ordering="stable_large"),
    "ollama": ProviderPackingProfile("ollama", chunk_tokens=250, ordering="dense"),
}

_STATUS_STABILITY = {
    "confirmed": 0,
    "candidate": 1,
    "stale": 2,
    "superseded": 3,
    "conflicted": 4,
    "archived": 5,
}
_VOLATILITY_STABILITY = {"low": 0, "medium": 1, "high": 2}


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


def _auto_provider_for_budget(token_budget: int) -> str:
    """Infer a provider strategy from the requested context size."""
    if token_budget <= 12_000:
        return "ollama"
    if token_budget <= 150_000:
        return "openai"
    if token_budget <= 350_000:
        return "anthropic"
    return "google"


def _get_provider_profile(provider: str | None, token_budget: int) -> ProviderPackingProfile | None:
    """Resolve an optional provider name to a packing profile."""
    if provider is None:
        return None
    normalized = provider.strip().lower()
    if not normalized:
        return None
    if normalized not in PROVIDERS:
        raise ValueError(f"Unknown provider '{provider}'. Choose from: {', '.join(PROVIDERS)}")
    if normalized == "auto":
        normalized = _auto_provider_for_budget(token_budget)
    return _PROVIDER_PROFILES[normalized]


def _render_claim_block(claim: Claim, score: float, output_format: str) -> str:
    """Render one claim in the requested output format."""
    if output_format == "text":
        return _claim_block_text(claim, score)
    if output_format == "xml":
        return _claim_xml(claim, score)
    return json.dumps(_claim_json_entry(claim, score))


def _build_blocks(ranked_rows: list[dict[str, Any]], output_format: str) -> list[_PackedBlock]:
    blocks: list[_PackedBlock] = []
    for row in ranked_rows:
        claim: Claim = row["claim"]
        score = float(row.get("score", 0.0))
        block = _render_claim_block(claim, score, output_format)
        blocks.append(_PackedBlock(block=block, row=row, tokens=estimate_tokens(block), score=score))
    return blocks


def _claim_sort_key(block: _PackedBlock) -> tuple[object, ...]:
    claim: Claim = block.row["claim"]
    return (
        claim.scope,
        claim.claim_type or "",
        _VOLATILITY_STABILITY.get(claim.volatility, 9),
        _STATUS_STABILITY.get(claim.status, 9),
        not claim.pinned,
        -block.score,
        claim.id,
    )


def _density_sort_key(block: _PackedBlock) -> tuple[object, ...]:
    claim: Claim = block.row["claim"]
    density = block.score / max(1, block.tokens)
    return (-density, not claim.pinned, -claim.confidence, block.tokens, claim.id)


def _order_blocks(blocks: list[_PackedBlock], profile: ProviderPackingProfile) -> list[_PackedBlock]:
    if profile.ordering == "stable_large":
        return sorted(blocks, key=_claim_sort_key)
    if profile.ordering == "dense":
        return sorted(blocks, key=_density_sort_key)
    return blocks


def _chunk_blocks(blocks: list[_PackedBlock], chunk_tokens: int) -> list[list[_PackedBlock]]:
    chunks: list[list[_PackedBlock]] = []
    current: list[_PackedBlock] = []
    current_tokens = 0

    for block in blocks:
        if current and current_tokens + block.tokens > chunk_tokens:
            chunks.append(current)
            current = []
            current_tokens = 0
        current.append(block)
        current_tokens += block.tokens

    if current:
        chunks.append(current)
    return chunks


def _pack_blocks(
    blocks: list[_PackedBlock],
    *,
    available: int,
    profile: ProviderPackingProfile | None,
) -> tuple[list[tuple[str, dict[str, Any]]], int]:
    used = 0
    included: list[tuple[str, dict[str, Any]]] = []
    if profile is None:
        chunks = [[block] for block in blocks]
    else:
        ordered = _order_blocks(blocks, profile)
        chunks = _chunk_blocks(ordered, profile.chunk_tokens)

    for chunk in chunks:
        chunk_tokens = sum(block.tokens for block in chunk)
        if used + chunk_tokens <= available:
            included.extend((block.block, block.row) for block in chunk)
            used += chunk_tokens
            continue
        for block in chunk:
            if used + block.tokens > available:
                continue
            included.append((block.block, block.row))
            used += block.tokens
    return included, used


def pack_context(
    ranked_rows: list[dict[str, Any]],
    *,
    token_budget: int = 4000,
    output_format: str = "text",
    provider: str | None = None,
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
    provider:
        Optional provider strategy. When omitted, preserves the historical
        greedy row-by-row behavior.

    Returns
    -------
    ContextResult with the formatted output and metadata.
    """
    if output_format not in OUTPUT_FORMATS:
        raise ValueError(f"Unknown format '{output_format}'. Choose from: {', '.join(OUTPUT_FORMATS)}")
    if token_budget <= 0:
        raise ValueError("token_budget must be positive.")
    profile = _get_provider_profile(provider, token_budget)

    claims_considered = len(ranked_rows)

    # Reserve tokens for header/footer framing.
    header, footer_template, overhead = _get_format_overhead(output_format)

    available = max(1, token_budget - overhead)
    blocks = _build_blocks(ranked_rows, output_format)
    included, used = _pack_blocks(blocks, available=available, profile=profile)

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

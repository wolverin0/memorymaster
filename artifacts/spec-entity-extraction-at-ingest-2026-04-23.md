# Spec — Entity extraction at ingest (#127 / Wave 3)

**Status:** draft, pending architecture sign-off.
**Author:** claude-session, 2026-04-23.
**Parent work:** supersedes the "Wave 1-A entity registry fix". Wave 1-A
raised `avg_aliases_per_entity` from 1.000 → 1.033 because existing data was
already consistent. The lift we actually need (≥2.5 aliases/entity) requires
extracting entities from *claim text*, not just `claim.subject`.

---

## Problem

`avg_aliases_per_entity = 1.033` — the registry is decorative. It holds one
alias per entity because every claim only registers its own `subject`
field. The rich content that distinguishes "unms-server-01" from
"aircontrol-ingest-service" lives in the free-text body and is never mined.

Consequence: entity-based retrieval gives the same result as scope+subject
retrieval. Typed relationships (#127 in the original v3.3.0 plan) cannot
exist because every entity has degree 1.

## Non-goals

- Replacing FTS5 lexical search.
- Building a knowledge graph with automatic edge inference.
- Real-time named-entity linking to external sources (Wikidata, etc.).

## Proposed design

### Layer 1 — deterministic pattern extractors (ship first)

Run a small set of regex extractors over `claim.text` at ingest time:

| Pattern class | Example match | Entity kind |
|---|---|---|
| File paths | `src/agents/auth.py` | `file` |
| Env vars | `GEMINI_API_KEY`, `MEMORYMASTER_WORKSPACE` | `env-var` |
| Hostnames/services | `unms-server-01`, `whatsappbot-prod` | `service` |
| Ports | `:8765`, `port 5432` | `port` |
| Commit SHAs | 7+ hex chars in commit context | `commit` |
| Tool names (from an allowlist) | `playwright`, `codex`, `mcp__memorymaster` | `tool` |

Pros: zero runtime cost, deterministic, easy to test. Enough to raise
`avg_aliases_per_entity` to ≈1.8 on a post-scan backfill.

### Layer 2 — LLM assist, gated by env flag

Optional second pass for claims where pattern extraction found nothing
interesting OR the text mentions ambiguous human names / product names.
Gated by `MEMORYMASTER_ENTITY_LLM=1` because it adds 300–800ms latency per
ingest.

Prompt returns `{entities: [{surface, kind, canonical_hint}]}`, bounded at
≤5 entities per claim. Cost cap: skip if we already extracted ≥3 entities
in Layer 1.

### Where it lives

- `memorymaster/entity_extractor.py` — new module, two functions:
  `extract_patterns(text)` and `extract_llm(text)`.
- `memorymaster/service.py::MemoryService.ingest` — hook after
  `resolve_or_create(conn, subject)` to register every extracted entity
  as an alias-of-self, then link via `entity_aliases`.
- `tests/fixtures/entity_extraction_eval.jsonl` — 100 labeled claims.

## Acceptance

1. On the current 11,703-claim corpus, a one-shot backfill raises
   `avg_aliases_per_entity` from 1.033 → ≥2.0 (Layer 1 only) or ≥2.5
   (both layers).
2. No live-ingest latency regression >5ms for Layer-1-only path.
3. Regression test: the 3 top entities (`server`, `workspace`, `service`)
   each split into at least 3 entities (e.g. `server` → `unms-server-01`,
   `omniclaude-server`, `git-server`).
4. Query `find_related_claims(entity_id)` returns >1 related claim for
   at least 40% of non-orphan entities.

## Estimate

- Layer 1: ~1 day. Pattern library + tests + backfill script + one-shot run.
- Layer 2: ~2 days (if cost/latency approved). LLM prompt + caching + fallback.
- Eval harness + labeled fixture: ~0.5 day.

## Open questions

- Do we store the raw `surface` strings as separate aliases, or only
  `canonical_hint`? (Recommendation: store both; surface has the original
  casing which matters for proper-noun disambiguation.)
- Back-pressure: if Layer-2 is enabled and Gemini throttles, do we queue
  or skip? (Recommendation: skip, log, flag the claim for later re-run.)
- Cross-claim linking: does an entity extracted from claim A automatically
  link to claim B that mentions the same surface? (Recommendation: yes,
  but gated by `claim.scope` — no cross-scope linking without explicit
  user action.)

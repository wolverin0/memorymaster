# MemoryMaster vNext governed capture implementation specification
# Covers: bounded delivery of universal capture, lineage, public verbs, ontology, and demo surfaces.
# Key terms: memorymaster.public.v1, capture jobs, claim evidence, edge supports, personal-v1.
# Read when: implementing or reviewing the vNext packages authorized by `ROADMAP.md`.
# Authority: this document implements `ROADMAP.md`; it is not a second roadmap.
# Boundaries: no live DB, scheduler activation, push, publish, Cognee dependency, or multi-user expansion.
# Verification: temporary databases and fake/local providers first; external gates remain explicit.

## Package boundaries

1. Baseline and architecture documentation.
2. Additive lineage and replay-safe capture-job storage.
3. Bounded adapters for text, references, documents, images, and audio.
4. `remember / recall / forget / improve` Python, CLI, and MCP parity.
5. `personal-v1` ontology and claim-backed graph support.
6. Capture Inbox, deterministic demo, onboarding, and release convergence.

## Invariants

- The claims database is authoritative.
- The sensitivity gateway runs before durable content writes.
- Captures acknowledge synchronously; LLM extraction is queued.
- Confirmation remains steward-owned.
- URL-only capture is `awaiting_evidence`.
- Local paths are private/local-trusted only and must stay inside configured roots.
- Retirement is logical; evidence and audit history survive.
- Trusted graph traversal requires at least one active, authorized claim support.
- Existing advanced surfaces remain compatible.

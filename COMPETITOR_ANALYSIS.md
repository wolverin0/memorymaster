# Competitor analysis: Cognee, claude-mem, and Serena
# Covers: product patterns worth adopting without copying competitor architecture or code.
# Key terms: Cognee, provenance, temporal search, tenants, graph, claude-mem, Serena.
# Read when: comparing MemoryMaster positioning or proposing a borrowed product pattern.
# Sources: official project documentation and repositories, checked 2026-07-27.
# Boundary: Cognee remains prior art and an optional benchmark reference, never a dependency.
# Differentiator: MemoryMaster keeps governed claims, lifecycle authority, citations, and steward review.

## Repositories Reviewed
- `topoteretes/cognee` (Apache-2.0)
- `thedotmack/claude-mem` (AGPL-3.0)
- `oraios/serena` (MIT)

## Corrected Cognee capability assessment

Cognee is not merely a vector-plus-graph demo. Its official architecture
documents a relational provenance layer for documents and chunks, vector and
graph stores, dataset-scoped permissions, users/tenants/roles, isolated
multi-user retrieval, and temporal search. Built-in document types include
text, PDF, audio, and image. Those are real capabilities and must not be used
as a false contrast for MemoryMaster.

Patterns adopted natively in MemoryMaster vNext:

- simple add/search/delete-style product verbs, expressed as governed
  `remember / recall / forget / improve`;
- universal capture adapters and asynchronous enrichment;
- relational provenance connecting source material to graph structure;
- a typed, versioned ontology and schema-validated graph extraction;
- local visualization and measurable retrieval/capture quality.

Patterns intentionally not adopted in this release:

- Cognee runtime code or dependencies;
- a second authoritative graph/vector truth layer;
- broad pluggable backend matrices, hosted operation, or multi-user expansion;
- graph-generated answers that bypass confirmed-claim rehydration.

Official references:

- `https://docs.cognee.ai/core-concepts/architecture`
- `https://docs.cognee.ai/core-concepts/multi-user-mode/permissions-system/overview`
- `https://docs.cognee.ai/core-concepts/multi-user-mode/multi-user-mode-overview`
- `https://docs.cognee.ai/guides/time-awareness`
- `https://docs.cognee.ai/core-concepts/building-blocks/datapoints`

## Observed Strengths

### claude-mem
- Real-time web viewer UX for memory stream.
- Progressive disclosure workflow (`search` -> `timeline` -> `get_observations`).
- Strong operator ergonomics around always-on usage.
- Explicit privacy convention with `<private>...</private>`.

Evidence:
- `.tmp_ext/claude-mem/README.md`
- `.tmp_ext/claude-mem/src/servers/mcp-server.ts`

### Serena
- Mature web dashboard with logs, stats, and control surfaces.
- First-class memory primitives (`list_memories`, `read_memory`, `write_memory`, `delete_memory`).
- Onboarding workflow for project bootstrapping and reusable memory files.
- Broad MCP client compatibility and strong integration docs.

Evidence:
- `.tmp_ext/serena/README.md`
- `.tmp_ext/serena/docs/01-about/035_tools.md`
- `.tmp_ext/serena/docs/02-usage/060_dashboard.md`
- `.tmp_ext/serena/src/serena/dashboard.py`

## Where MemoryMaster Is Already Strong
- Reliability-first claim lifecycle (stale/superseded/conflicted with transitions).
- Deterministic + policy-driven revalidation and archival flow.
- Synthetic/adversarial eval harness + deterministic operator E2E harness.
- MCP + CLI with explicit review queue and operator checkpointing.

## Gap map to our roadmap

- Capture ergonomics -> `ROADMAP.md` Now: public verbs and universal capture.
- Provenance gap -> `ROADMAP.md` Now: authoritative lineage tables.
- Graph integrity gap -> `ROADMAP.md` Now: personal-v1 and active supports.
- UI gap -> `ROADMAP.md` Now: Capture Inbox and disposable demo.
- Hosted/team/backend breadth -> `ROADMAP.md` Later, only on explicit demand.

## Adoption Strategy (What to Borrow)
- Borrow UX patterns, not code:
  - dashboard panels
  - progressive retrieval interaction
  - memory curation flows
- Keep MemoryMaster model authoritative:
  - claim lifecycle remains the core abstraction
  - citations and auditability stay mandatory

## Licensing and Reuse Guardrails
- `Cognee` is Apache-2.0, but this release adopts documented behavior only and
  introduces no Cognee code or dependency.
- `claude-mem` is AGPL-3.0: do not copy code into this project unless we intentionally adopt AGPL obligations.
- `Serena` is MIT: permissive reuse is possible with attribution, but direct copy should still be avoided in favor of native implementation.
- Prefer reimplementation from behavior/spec, with references and tests.

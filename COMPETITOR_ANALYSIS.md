# Competitor Analysis: claude-mem and Serena

As of 2026-03-03.

## Goal
Map useful patterns from mature memory/tooling projects into MemoryMaster without copying architecture blindly.

## Repositories Reviewed
- `thedotmack/claude-mem` (AGPL-3.0)
- `oraios/serena` (MIT)

Local clones used:
- `.tmp_ext/claude-mem`
- `.tmp_ext/serena`

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

## Gap Map to Our Roadmap
- UI gap -> `ROADMAP.md` Track A (`A1`..`A11`)
- Connector gap -> `ROADMAP.md` Track B (`B2`..`B6`)
- Reliability hardening gap -> `ROADMAP.md` Track C (`C1`..`C7`)
- Active updater vision -> `ROADMAP.md` Track E (`E1`..`E8`)
- Operability ecosystem gap -> `ROADMAP.md` Track D (`D1`..`D6`)

## Adoption Strategy (What to Borrow)
- Borrow UX patterns, not code:
  - dashboard panels
  - progressive retrieval interaction
  - memory curation flows
- Keep MemoryMaster model authoritative:
  - claim lifecycle remains the core abstraction
  - citations and auditability stay mandatory

## Licensing and Reuse Guardrails
- `claude-mem` is AGPL-3.0: do not copy code into this project unless we intentionally adopt AGPL obligations.
- `Serena` is MIT: permissive reuse is possible with attribution, but direct copy should still be avoided in favor of native implementation.
- Prefer reimplementation from behavior/spec, with references and tests.

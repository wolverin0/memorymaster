# MemoryMaster upstream prior-art delta — 2026-08-03
# Covers: refreshed third-party clones, release deltas, and patterns applicable to MemoryMaster.
# Key terms: coverage ledger, pagination, provider resilience, producer identity, OAuth evaluation.
# Read when: selecting reliability work after governed capture vNext or refreshing upstream clones.
# Authority: research input to `ROADMAP.md`; this report does not create or reorder roadmap commitments.
# Scope: behavior and release-note analysis only; no third-party runtime or copied code is introduced.
# Verdict: five bounded patterns merit implementation; cloud, daemon, agentic-retrieval, and backend breadth stay deferred.

## Executive decision

The refresh found useful work, but no reason to replace MemoryMaster's governed-claims
architecture. The best ideas are reliability contracts around the system we already
shipped: make incomplete extraction visible, prove every bounded enumeration is
complete, report partial provider failures honestly, normalize producer identities,
and make the evaluation harness usable through subscription/OAuth-backed agents.

Recommended order:

1. **P0 — capture and graph coverage ledger.** Add a local, content-free diagnostic
   view that distinguishes accepted, pending, retryable, blocked, completed, and
   missing-stage items. Include eligible-but-unextracted claims and graph supports.
   A green health result must mean every accepted item is terminal or actively due,
   not merely that the worker exited zero. This combines codebase-memory-mcp's
   explicit missed graph, gbrain's silent-failure doctor checks, and Cognee's
   actionable ingestion diagnostics. [codebase-memory-mcp release](https://github.com/DeusData/codebase-memory-mcp/releases/tag/v0.9.1-rc.1),
   [gbrain release](https://github.com/garrytan/gbrain/releases/tag/v0.42.70.0),
   [Cognee release](https://github.com/topoteretes/cognee/releases/tag/v1.4.1)
2. **P0 — exactly-once enumeration tests.** Pin stable cursors, honest totals, and
   complete traversal for public/admin lists and any bulk retirement or reconciliation
   operation. Mem0 fixed `delete_all()` leaving records beyond the first vector-store
   page; codebase-memory-mcp added exactly-once pagination. MemoryMaster's source
   retirement is currently one SQLite query rather than a paged backend call, so the
   immediate deliverable is a regression contract, not a rewrite.
   [Mem0 release](https://github.com/mem0ai/mem0/releases/tag/v2.0.15),
   [codebase-memory-mcp release](https://github.com/DeusData/codebase-memory-mcp/releases/tag/v0.9.1-rc.1)
3. **P0 — honest partial-failure semantics.** Structured-output parse failures,
   unparseable success responses, embedding chunk failures, and provider exhaustion
   must produce completed/retryable/blocked counts that cannot look fully successful.
   Preserve successful items, retry only the missing work, and never silently change
   providers. GitNexus now retries unparseable HTTP 200 embedding responses and
   survives partial failures; gbrain changed incomplete embedding/extraction runs
   from silent green to visible failure. [GitNexus delta](https://github.com/abhigyanpatwari/GitNexus/compare/a7df8f861a5f41b0bd1b504f0e4c19bf84c0559c...561f913a32b9cd515f76756c447beb5c721bd424),
   [gbrain release](https://github.com/garrytan/gbrain/releases/tag/v0.42.69.0)
4. **P1 — OAuth-backed evaluation runner.** Add an optional evaluation adapter that
   invokes an already authenticated headless Codex/OpenCode session, records the exact
   model and effort, and compares judge variants. It must remain evaluation-only: no
   steward promotion, production extraction, or runtime dependency on a subscription
   TUI. Agent Skill Creator added keyless subscription-based judging and model/cost
   comparison rollouts. [keyless judge commit](https://github.com/FrancyJGLisboa/agent-skill-creator/commit/418236f),
   [model comparison commit](https://github.com/FrancyJGLisboa/agent-skill-creator/commit/333bc3a)
5. **P1 — canonical producer actor identity.** Extend producer envelopes with optional
   canonical actor id/name/handle fields and per-producer precedence rules. Keep raw
   metadata as evidence, but make the chosen identity explicit and tested. Zep's first
   hardened ingest release fixed Slack authors by choosing `real_name` rather than the
   mutable display name. [Zep release](https://github.com/getzep/zep/releases/tag/zep-ingest-v0.1.0)

## Useful later, not immediate

- **Streaming document extraction.** Cognee now streams large-file ingestion. A
  bounded streaming/spooled implementation could reduce peak memory for MemoryMaster's
  25 MiB local-document allowance, but the present personal limit makes this P2 rather
  than urgent. [Cognee v1.4.1](https://github.com/topoteretes/cognee/releases/tag/v1.4.1)
- **Compact admin/MCP list profiles.** codebase-memory-mcp reports large token savings
  from compact tree output. MemoryMaster recall is already token-budgeted, so target
  verbose list, audit, and graph-inspection tools rather than altering the public-v1
  receipt by default. [codebase-memory-mcp v0.9.1 RC](https://github.com/DeusData/codebase-memory-mcp/releases/tag/v0.9.1-rc.1)
- **Unicode identity/entity fixtures.** gbrain repaired non-ASCII mention extraction.
  Add Spanish names, diacritics, and non-Latin fixtures when the entity evaluation set
  is next expanded. [gbrain v0.42.69.0](https://github.com/garrytan/gbrain/releases/tag/v0.42.69.0)
- **Incremental graph provenance invariants.** Graphify made replacement tier-aware,
  preserved directionality, and restored origin markers during rebuilds. MemoryMaster
  already stores claim-backed edge supports, but equivalent rebuild/re-extraction
  invariants are worth retaining in graph tests. [Graphify v0.9.32](https://github.com/Graphify-Labs/graphify/releases/tag/v0.9.32)
- **MCP major-version compatibility fixture.** Graphify verified full stdio handshakes
  under MCP SDK 1.x and 2.x. Add a compatibility fixture only when MemoryMaster plans
  its own MCP dependency-major transition. [Graphify v0.9.31](https://github.com/Graphify-Labs/graphify/releases/tag/v0.9.31)

## Already present — retain and test, do not duplicate

- The ontology prompt is generated from the loaded registry, unknown entity/relation
  values fail closed, and job diagnostics preserve the rejection. claude-mem's recent
  under-emitted type bug is evidence for a prompt/registry parity regression test, not
  a new ontology layer. [claude-mem v13.13.0](https://github.com/thedotmack/claude-mem/releases/tag/v13.13.0)
- Capture jobs already have bounded attempts, exponential retry, expired-lease
  recovery, blocked codes, redacted error details, and terminal status counts.
- The Capture Inbox already shows stage/status/error code, evidence, claims,
  relationships, and retirement preview/apply. The proposed coverage ledger adds
  completeness and missing-stage accounting rather than another inbox.
- Local capture locators are root-relative and symlink-contained. Graphify's portable
  root-relative node-id fixes reinforce this existing boundary.
  [Graphify v0.9.29](https://github.com/Graphify-Labs/graphify/releases/tag/v0.9.29)
- Confirmed-claim graph extraction already deduplicates supports, preserves ontology
  directionality, excludes retired/unauthorized support, and rehydrates through the
  claim store.
- Windows scheduled actions already use hidden process creation and durable logs.
  Upstream fixes in Graphify and claude-mem validate the importance of the existing
  regression gate rather than adding another scheduler.
- Release-truth generation and package-content tests already guard generated artifacts.
  claude-mem's shipped-bundle freshness fix suggests keeping that gate mandatory.
  [claude-mem v13.13.0](https://github.com/thedotmack/claude-mem/releases/tag/v13.13.0)

## Explicit rejects and deferrals

- **No coordination daemon.** codebase-memory-mcp's per-user daemon solves a different
  high-concurrency graph-index problem. MemoryMaster keeps SQLite plus the existing
  hourly scheduler; a new resident service would add lifecycle and security burden.
- **No hosted/multi-user write fences.** gbrain's OAuth slug-prefix fences are sound
  for shared sources, but team operation remains deferred and SQLite-only is the
  selected release profile. [gbrain v0.42.72.0](https://github.com/garrytan/gbrain/releases/tag/v0.42.72.0)
- **No agentic retrieval decomposition.** Zep shipped then reverted its agentic
  retrieval evaluation and documented the single-shot boundary. MemoryMaster keeps
  graph retrieval as a candidate signal, not a graph-answer agent.
  [Zep v0.1.0](https://github.com/getzep/zep/releases/tag/zep-ingest-v0.1.0)
- **No default-model chase.** Mem0 changed its reranker default to `gpt-5-mini` and
  other projects standardized examples on current models. MemoryMaster must choose
  providers/models through its configured budget and measured quality gates, not an
  upstream default. [Mem0 v2.0.15](https://github.com/mem0ai/mem0/releases/tag/v2.0.15)
- **No Cognee, Mem0, Zep, graph database, Telegram, OpenClaw, or cloud runtime
  dependency.** Borrow documented behavior and write native governed tests.
- **No Qdrant/BM25 expansion while Qdrant is quarantined.** Mem0's server-side Qdrant
  keyword search is interesting only after MemoryMaster's optional semantic profile
  passes its existing authority, scope, and recall-regression gates.

## Refreshed clone inventory

All commit counts are the exact old-local snapshot to the reviewed target on
2026-08-03. Clean current branches were fast-forwarded. No original document,
database, package, or live scheduler was changed by this refresh.

| Repository | Old -> reviewed target | Commits | Latest relevant state | Decision |
|---|---:|---:|---|---|
| agent-skill-creator | `0663e3ef` -> `006901a0` (`origin/main`) | 121 | `v6.0.0-30-g006901a` | Adopt OAuth judge/model-rollout ideas. Local branch was preserved because upstream rewrote history. [delta](https://github.com/FrancyJGLisboa/agent-skill-creator/compare/0663e3ef8a23be6cebb87b8a35c5fbfce94a4f9a...006901a0ed175cb96b6743fb920080bf8004ac75) |
| claude-mem | `3fe0725a` -> `b368abae` | 2,363 | v13.13.0 plus one commit | Add registry/prompt and generated-artifact freshness tests; do not copy AGPL code. [delta](https://github.com/thedotmack/claude-mem/compare/3fe0725a97e18b5edf3e61cde60e181ab2b6c997...b368abaeabfebb8d5cfe18836b779edda204664c) |
| codebase-memory-mcp | `7824e505` -> `d6be58ef` | 1,967 | v0.9.1-rc.1 plus 79 commits | Highest-value source for coverage, compact output, exact pagination, and store integrity. [delta](https://github.com/DeusData/codebase-memory-mcp/compare/7824e505c192023a21b3e90bcb98ca6210629b64...d6be58ef9d43c574a2d1b0827ecc1e3c4846f0fe) |
| Cognee | `19132718` -> `38eece5b` | 8,956 | v1.4.1 plus 11 commits | Adopt diagnostics/streaming patterns only; keep it out of runtime. [delta](https://github.com/topoteretes/cognee/compare/1913271821c84cec1630dd5b15ceb17dee8ace55...38eece5bbb0cb9f5706fed908abd16dba0f5505e) |
| gbrain | `814258dd` -> `82fe0216` | 516 | v0.42.72.1 plus 6 commits | Adopt fail-loud doctor/completeness patterns; defer shared-user features. [delta](https://github.com/garrytan/gbrain/compare/814258dda67945ffec9457a1e73980e947b7e462...82fe0216ff04e4b1e898a1062d3abe6487fa8383) |
| GitNexus | `a7df8f86` -> `561f913a` | 422 | v1.6.10 RC stream | Adopt deterministic/partial-failure test patterns; it remains development tooling. [delta](https://github.com/abhigyanpatwari/GitNexus/compare/a7df8f861a5f41b0bd1b504f0e4c19bf84c0559c...561f913a32b9cd515f76756c447beb5c721bd424) |
| Graphify | `544f95ef` -> `00efd6e7` | 451 | v0.9.32 | Retain provenance/directionality/rebuild invariants; it remains development tooling. [delta](https://github.com/Graphify-Labs/graphify/compare/544f95efa65c56af2cdc22cad14750839005e76d...00efd6e7969837ae4a9f11d8d504dcd3b20b09df) |
| Letta | `6d8cb7fd` -> `ff19ffea` | 3 | documentation/policy only | No applicable product delta. [delta](https://github.com/letta-ai/letta/compare/6d8cb7fd48938b629aad5770faa051a8d42e1e9f...ff19ffeafeb54bd2a7dc5d4a552f10191732a235) |
| Mem0 | `b2ff3aed` -> `c90bdbdc` | 123 | Python SDK v2.0.15 | Adopt complete-enumeration regression tests; defer backend-specific breadth. [delta](https://github.com/mem0ai/mem0/compare/b2ff3aeda5375c9354ae6b0cf9c9d78f101344d0...c90bdbdce078f46d768c44031ce77a1b93dbc3f6) |
| mempalace | `afd04288` -> `afd04288` | 0 | unchanged | No delta to assess. |
| My-Brain-Is-Full-Crew | `238ae8c` -> `238ae8c` | 0 | unchanged | No delta to assess. |
| Zep | `826c5492` -> `1c581922` | 22 | zep-ingest v0.1.0 plus 3 commits | Adopt producer identity precedence; retain single-shot retrieval. [delta](https://github.com/getzep/zep/compare/826c5492d9cc3a7caf92a9870529f29b5a8546e3...1c58192266ef648cc2db7fe2cbf684cc02a366f9) |

`cloned/MyBrain` remains an empty non-repository directory and was not treated as
an upstream project.

## Safe implementation slices

This report proposes roadmap input only. If accepted into `ROADMAP.md`, implement in
small SQLite-only packages:

1. Coverage read model, CLI/MCP `doctor` surface, Capture Inbox summary, and adversarial
   tests for orphaned/missing-stage work. Diagnostics expose identifiers/counts/codes,
   never source or claim text.
2. Cursor/enumeration contract tests across SQLite list and retirement/reconciliation
   paths, including more rows than one page and stable tie ordering.
3. Provider partial-failure fixtures covering malformed success, timeout, quota,
   one-bad-chunk, expired lease, and restart. Assert source preservation and honest
   terminal counts.
4. Evaluation-only headless-agent adapter with explicit model/effort provenance,
   deterministic fixture fallback, opt-in execution, and no production write path.
5. Additive producer actor fields with WhatsApp/Hermes/direct-agent fixtures and a
   documented precedence rule. Do not infer a person's canonical identity from an
   unverified display name.

Each slice must pass the existing recall-regression, scope, sensitivity, replay,
SQLite restore, Ruff, and release-truth gates. No implementation should be copied from
the reviewed repositories without a separate license and provenance review.

## Refresh policy

Repeat this audit quarterly or before a major MemoryMaster roadmap revision. Fetch
first, fast-forward only clean non-diverged clones, preserve local divergence, record
old/new SHAs, read release notes and actual changed commits, then classify each idea as
adopt, retain, investigate, defer, or reject. Never turn clone refresh into a runtime
dependency update automatically.

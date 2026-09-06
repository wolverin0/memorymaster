<!-- doc-head: upstream research refresh with 17 pinned snapshots and bounded improvement proposals -->
# Upstream comparison â€” 2026-09-05
Covers: upstream growth, source-inspected advantages, MemoryMaster gaps and recommended experiments.
Key terms: Dreaming recall, quota cooldown, context delivery, temporal retrieval, source freshness.
Read when: choosing the next small improvement; this is research input, not a competing roadmap.
Status: stated source coverage and snapshot manifest verified; no upstream runtime benchmark or production change.
<!-- /doc-head -->

## Bottom line

Yes, the old comparison needed refreshing. Seventeen public repositories are now
cloned under `repo/`; fourteen old `cloned/` repositories remain untouched.
This includes three new baselines: Graphiti, Serena and the active Letta Code
successor. All snapshots have exact SHAs, default branches and canonical origins
in [the machine-readable manifest](UPSTREAM-SNAPSHOTS-2026-09-05.json).
The empty MyBrain directory was not counted.

The useful opportunities are **better measurement of what Dreaming misses,
less repetitive context, restart-safe quota handling, and source-change-aware
recall**. That is a shortlist, not a recommendation to absorb seventeen frameworks.
Our priority should remain the operator's personal cross-agent workflow, governed
SQLite and Gemini-only deployment. Enterprise scale, connectors, funding and
marketing claims were deliberately outside this engineering review.

We cannot honestly say another system has higher accuracy on our workload:
**no common-corpus head-to-head benchmark was run**. Below, â€œadvantageâ€ means an
inspected mechanism or product surface worth testing, not a measured superiority
claim.

## Snapshot and coverage inventory

Counts are upstream-only commits reachable from the fetched default branch versus
the saved local SHA, including merges. They are not feature counts, stable-release
counts or a uniform â€œlast monthâ€ interval. OpenViking/openwiki have later saved
baselines than the August 3 report. Each clone retains commit history through a
filtered single-branch clone; source blobs for the checked-out tree are present.

| Project | Snapshot / branch | Change since baseline | Relevant result |
|---|---|---|---|
| agent-skill-creator | `46191ce2` / main | +79 vs prior report; divergent local clone | Renamed Agent Skills Platform; workflow-to-skill packaging and evidence-linked maintenance. Screening only; do not duplicate our governed-skills sidecar. [Source 1](#sources) |
| claude-mem | `b6e05382` / main | +154 | Restart-persistent quota cooldown and one-probe recovery; compact search -> timeline -> selected detail. Useful reliability/UI patterns. [Source 2](#sources) |
| codebase-memory-mcp | `fe85a6b2` / main | +883 | Native code intelligence plus lossless compact output with measured activation thresholds. Adjacent developer tool, not a claims-store replacement. [Source 3](#sources) |
| cognee | `78ff5765` / main | +1291 | Feedback-weighted graph enrichment and session pipelines. Broader learning loop; usefulness feedback must not become truth authority. [Source 4](#sources) |
| gbrain | `8c70f625` / master | +212 | New buried-signal Dreaming rescue, quote verification/repair and weak-lexical fusion handling. Highest-value write/read-quality comparison. [Source 5](#sources) |
| gitnexus | `a049b2da` / main | +252 | Workflow/review evolution now has repeat-run promotion policy and provenance. Retain as development tool; not another production memory engine. [Source 6](#sources) |
| graphify | `c9f99018` / v8 | +320 | Entity-dedup safeguards and provenance fixes. Useful alias proposals and negative fixtures, not fuzzy observation-component membership. [Source 7](#sources) |
| graphiti | `54742286` / main | new baseline | Temporal edge filtering and configurable hybrid retrieval recipes. Stronger temporal query surface; no new database justified yet. [Source 8](#sources) |
| letta | `4511fa0b` / main | +3 | V1 server retired; main now redirects to Letta Code. Three commits here conceal a project migration, not dormancy. [Source 9](#sources) |
| letta-code | `7a4337e2` / main | new baseline | Active successor: explicit reflection triggers and memory/history UX. Different product: agent harness, not cross-provider governed memory service. [Source 10](#sources) |
| mem0 | `dae67f74` / main | +80 | New Dream preview/runs/sources API documentation and integrations. Dream service implementation is not demonstrated by the OSS core inspected. [Source 11](#sources) |
| mempalace | `d9f05907` / develop | +509 | Light MCP interface consolidates tool surface with explicit underlying write preflight. Interesting simplification, not a reason to add a query DSL. [Source 12](#sources) |
| My-Brain-Is-Full-Crew | `238ae8c4` / main | +0 | Unchanged baseline. Obsidian workflow reference; no fresh engineering delta. [Source 13](#sources) |
| OpenViking | `0c5147ca` / main | +209 | Hierarchical context assembly and server-side cross-turn recall ledger. Ledger already existed in our saved baseline; not a newly introduced feature. [Source 14](#sources) |
| openwiki | `1e6d54cd` / main | +54 | New sparse claim reconciliation on top of resolver-owned source versions. Repository-evidence freshness is the relevant pattern, not replacing SQLite with Markdown. [Source 15](#sources) |
| serena | `13ac8c5b` / main | new baseline | Recent atomic memory-write repair, including permission preservation. Useful filesystem regression pattern; our profile writer already uses replace. [Source 16](#sources) |
| zep | `54f63eeb` / main | +9 | Examples/integrations, not the hosted product implementation. Submission batching changed; Graphiti is reviewed separately. [Source 17](#sources) |

All seventeen clean checkouts were inspected at the pinned revisions. Review depth
is explicitly recorded in the manifest: targeted source sections for thirteen,
history/documentation screening for four. No upstream installer, dependency
installation, model inference, external database or upstream test suite was run.

### Corrections to the old inventory

- **Letta:** GitHub reports `archived=false`, but the README explicitly retires the
  V1 server to an archive branch and points to `letta-code`. Repository status
  flags alone would mislead this review. [Source 9](#sources), [Source 10](#sources)
- **Renames:** agent-skill-creator redirects to **agent-skills-platform**;
  safishamsi/graphify redirects to **Graphify-Labs/graphify**; MemPalace's canonical
  organization is now **MemPalace**. Local folder names remain stable; canonical
  URLs are in the manifest. [Source 1](#sources), [Source 7](#sources), [Source 12](#sources)
- **History:** Agent Skills Platform's old local clone has 18 baseline-only and
  165 upstream-only commits. Against the SHA actually used by the August report,
  the forward change is 79. Calling this simply â€œ165 new commitsâ€ would be wrong.
  [manifest](UPSTREAM-SNAPSHOTS-2026-09-05.json)
- **Branches:** Graphify defaults to `v8`; MemPalace defaults to `develop`.
  These are research snapshots, not endorsements to upgrade installed tools to
  unreleased/default-branch code. [Source 7](#sources), [Source 12](#sources)
- **Source scope:** Zep's repository is examples/integrations, not the hosted
  product; Mem0's new Dream endpoints are API documentation, not proof that the
  synthesis backend is available in the inspected OSS implementation.
  [Source 17](#sources), [Source 11](#sources)
- **License labels:** inspected root files show Apache-2.0 for claude-mem,
  AGPL-3.0 for OpenViking, and PolyForm Noncommercial 1.0.0 for GitNexus.
  GitHub metadata returns NOASSERTION for GitNexus and Crew; Crew's root file
  says MIT. Record exact applicable files before any reuse; no upstream code was
  copied into MemoryMaster. These labels are inventory, not a legal compatibility
  determination. [manifest](UPSTREAM-SNAPSHOTS-2026-09-05.json)

## What is worth learning, and what we already have

### 1. Dreaming: measure missed knowledge as well as bad additions

**Upstream:** gbrain's new triage rescue only admits a borderline transcript when
its type is eligible and it contains enough distinct, substantive,
mechanically verified quote segments. It has fixtures for fabricated, repeated
and short quotes. Its quote-repair pass also has explicit size/probe bounds.
[Source 5](#sources)

**MemoryMaster:** the new source steward already checks exact quotes, source
fingerprint, chronology, modality, scope, specificity and privacy before accepting
a candidate. Reimplementing this as another LLM â€œsteward sessionâ€ would duplicate
work. The September assessment covered **88 emitted add actions**, not the
population of rejected or never-extracted source passages. It therefore provides
diagnostics about emitted content, not extraction recall.
[local source](../memorymaster/dreaming/source_review.py),
[dated assessment](ENGINEERING-CONSOLIDATION-2026-09-05.md)

**Recommended:** use the existing review harness to label a bounded source sample
including mixed sessions and zero-output sessions. Ask â€œwhich useful facts should
have been captured?â€ before changing thresholds. Track accepted useful facts,
unsupported additions and missed facts separately. Keep exactly the same Gemini
budget and steward boundary for any later rescue experiment.

**Do not copy blindly:** gbrain's repair pass can strip quote marks from
ungrounded prose and is fail-open; it also excludes technical content from this
particular rescue. Neither policy fits our governed coding-memory intake as-is.
An exact quote is also not proof that the claim follows from it.

### 2. Retrieval: prevent weak matches from overwhelming strong evidence

**Upstream:** gbrain now marks relaxed lexical matches and excludes those votes
when a nonempty text-vector arm is available, retaining fallback when it is not.
It also changes cache/diagnostic behavior for degraded results. Graphiti exposes
multiple hybrid retrieval/reranking recipes and temporal edge filters.
[Source 5](#sources), [Source 8](#sources)

**MemoryMaster:** our default legacy path is lexical, with relevance separated
from confidence/freshness boosts and a relevance gate; optional vector mode uses
weighted scoring. It is **not the same RRF pipeline**, so transplanting gbrain's
condition would not establish a fix. Existing temporal claim fields also mean we
do not need to invent timestamps before exploring historical graph questions.
[ranking source](../memorymaster/recall/retrieval.py),
[claim model](../memorymaster/core/models.py)

**Recommended:** compare current ranking with one narrowly changed scorer on the
same questions, including rare names, Spanish paraphrases, older valid facts,
corrections and unavailable-vector fallback. Only retain an improvement that
preserves authorization and ordinary recall behavior. Graphiti's historical query
surface is a later experiment, not a justification for adding Neo4j or replacing
our deterministic observation components.

**Measurement warning:** the old optional CI evaluator still points to missing
case files and uses `continue-on-error: true`. This is a concrete harness defect,
not proof that all retrieval tests are broken. Fix the reference to real fixtures
or remove the obsolete step before citing it as quality evidence.
[CI](../.github/workflows/ci.yml),
[evaluator defaults](../scripts/eval_memorymaster.py),
[existing dated evidence](ENGINEERING-CONSOLIDATION-2026-09-05.md)

### 3. Context delivery: our installed fix must survive reinstallation

**Upstream:** claude-mem exposes compact search, surrounding timeline and selected
detail retrieval. OpenViking's server-side recall ledger tracks served URIs and
turn distance; malformed/missing ledger state degrades to no deduplication.
The latter is a retained capability, not a new delta in our saved OpenViking
snapshot. [Source 2](#sources), [Source 14](#sources)

**Concrete local finding:** the installed recall hook contains known-machine-event
filtering and a session-scoped briefing fingerprint. The packaged hook template
contains neither; it only checks query length before recall. `install_hooks`
writes each template over the destination. Thus reinstalling through that path can
remove the installed safeguards. This was established by read-only inspection;
no installation was executed.
[packaged hook](../memorymaster/config_templates/hooks/memorymaster-recall.py),
[installer](../memorymaster/surfaces/setup_hooks.py)

**Recommended first fix:** consolidate the existing guard into the supported
template and cover installation round-trip. Acceptance: machine event is skipped;
a genuine question containing the same marker mid-sentence still recalls;
identical briefing is not reinjected within the intended session window;
changed briefing and new session still inject. Define compaction/reset behavior
explicitly so deduplication does not withhold context after it was evicted.

Do not build a separate context ledger first: repair the implementation/install
split, then measure whether per-item cross-turn dedup is still needed.

### 4. Provider quotas: persistent cooldown differs from a per-cycle budget

**Upstream:** claude-mem persists the provider's exhausted-quota window across
worker restart, keeps the in-flight probe claim process-local and admits one
post-cooldown probe. It distinguishes durable provider state from ownership that
died with the process. [Source 2](#sources)

**MemoryMaster:** the inspected `cycle_scope` creates a new in-memory budget;
Google key cooldown is also tracked in a process-local rotator. These controls
already bound each cycle/call but do not themselves implement the persistent
provider window above. This is a design delta, **not a measured current retry
storm**; job backoff and other deployment controls must be considered before
adding state.
[cycle budget](../memorymaster/core/llm_budget.py),
[key rotation](../memorymaster/core/key_rotator.py),
[provider](../memorymaster/core/llm_provider.py)

**Recommended:** first inspect the existing refusal/retry history for repeated
quota failures across cycles. If present, add one bounded cooldown record to an
existing persistence surface, never a new service or provider fallback. Test
restart during cooldown, simultaneous recovery attempts, stale probe and recovery.
Gemini remains the only deployment provider selected by the operator.

### 5. Source freshness and feedback: promote usefulness, not self-confirmation

**Upstream:** OpenWiki resolves source versions itself rather than accepting
model-supplied hashes; it can relocate line ranges and separately report changed,
missing and unresolved evidence. Sparse claim reconciliation is new in this
delta. Cognee has an explicit session-feedback pipeline that updates graph
weights and records processing state. [Source 15](#sources), [Source 4](#sources)

**MemoryMaster:** immutable source/claim support fingerprints and retirement
invalidation already cover governed observations. A repository-line resolver is
a narrower, different capability than â€œwe need provenance.â€ Feedback must not
increase a claim's factual authority merely because a generated answer reused it.
[observation implementation](../memorymaster/knowledge/graph_observations.py),
[source steward](../memorymaster/dreaming/source_review.py)

**Recommended:** only if code-file facts demonstrably stay stale, extend the
existing producer/source boundary with version-aware reconciliation. Pilot on
one repository and distinguish edited content from line movement or an unreadable
file. Human correction/usefulness feedback can inform ranking experiments; it
must not bypass steward confirmation or create generated-evidence loops.

### 6. Smaller surface, clearer diagnostics, no extra language

**Upstream:** MemPalace's light MCP surface dispatches through underlying-tool
preflight even for direct calls. codebase-memory-mcp provides compact table output
and only activates prefix compression when its measured size conditions hold.
Letta Code separates disabled reflection, compaction and step-count triggers.
[Source 12](#sources), [Source 3](#sources), [Source 10](#sources)

**Recommended:** measure our tools/list and repeated administrative result sizes,
then offer an optional small agent-facing surface backed by the existing public
facade if those costs matter. Show Dreaming input count, rejected/accepted/no-signal
outcomes and source links together. Mem0's documented preview/run/source views
are a useful UX reference, but not backend evidence. [Source 11](#sources)

**Do not adopt:** another query DSL, an always-on reflection agent, another memory
database or dozens of connectors. Likewise, Graphify's fuzzy entity matching may
suggest aliases; it must never decide membership of governed observation
components. Serena's atomic-write fix is useful negative-test inspiration, while
our compiled-profile writer already uses temporary-file replacement.
[Source 7](#sources), [Source 16](#sources),
[existing profile writer](../memorymaster/profile/engine.py)

## Recommended next work â€” deliberately small

These are proposals subordinate to `ROADMAP.md`, not new approved milestones.

| Order | Work | Evidence / exit | Scope |
|---|---|---|---|
| 1 | Preserve installed recall guards in package/setup; retire or repair the obsolete optional eval entry | Installation round-trip preserves guards; referenced eval cases exist and missing fixtures cannot look successful | Two small, separate fixes; no schema or new service |
| 2 | Measure Dreaming omissions using source windows, not only emitted claims | Review includes zero-output and mixed sessions; useful expected facts matched to outputs; precision and omission counts reported separately | Existing offline harness; no automatic data curation |
| 3 | Test one retrieval/context improvement | Same-corpus paired results including degradation and scope controls; keep only measured improvement | Current engine, feature-gated; no backend migration |
| Later, only if demonstrated | Persistent quota cooldown; source-line freshness; compact MCP view | Respectively: repeated quota history, stale code-fact cases, or measured schema/context overhead | Reuse existing structures |

There is enough prior emitted-data evidence to start. Waiting another 24 hours is
not an implementation prerequisite, and collecting more job exits will not supply
the missing human/independent expected-fact labels.

## Verification and limits

- Branch freshness: main was zero commits behind origin/main at start;
  comparison baseline is `5e701ab3b43ae878d47697e55924572a6701ceed`.
- All 17 new clone commands exited zero; each recorded SHA/branch and clean status
  was checked. Old clone heads were only read, never fetched/reset/cleaned.
- Manifest validation passed: 17 snapshot SHAs/branches/clean states, 34 named
  evidence files and unchanged old heads; 18 local report links resolve.
  Git whitespace validation passed and the snapshot directory is ignored.
- Public source inspection only: no upstream tests or comparative accuracy/latency
  benchmarks executed; no foreign installer or dependency code executed.
- No production code, service, scheduler, provider configuration or existing
  claim history changed. Hook-template drift was recorded as candidate
  `mm-3cf1` through governed intake; it was not self-confirmed or used to
  authorize remediation. That one research claim is not historical curation.
- `repo/` is ignored to prevent accidental vendoring. Only this report, its
  manifest, doc-map entry and the narrow ignore rule are intended tracked edits;
  unrelated `delta-exchange/` is preserved.
- No release, merge, deployment, feature-success watermark or scheduled task was
  created for this research.
- The competitor-analysis skill shaped source attribution, comparison against our
  actual implementation, uncertainty labels and the short recommendation list;
  commercial-market analysis was intentionally outside the request.

## Sources

Pinned source URLs are reproducible; line numbers may differ from future main.
The manifest records exact local baselines and current canonical metadata.
Reported upstream benchmark numbers were not used to rank products.

- Source 1: [FrancyJGLisboa/agent-skills-platform, pinned snapshot](https://github.com/FrancyJGLisboa/agent-skills-platform/tree/46191ce27d90011b2414addb877d246af2daa10f); inspected: [README.md](https://github.com/FrancyJGLisboa/agent-skills-platform/blob/46191ce27d90011b2414addb877d246af2daa10f/README.md).
- Source 2: [thedotmack/claude-mem, pinned snapshot](https://github.com/thedotmack/claude-mem/tree/b6e05382e2be35e29f22335f04a6890a4c0ba976); inspected: [src/shared/quota-cooldown.ts](https://github.com/thedotmack/claude-mem/blob/b6e05382e2be35e29f22335f04a6890a4c0ba976/src/shared/quota-cooldown.ts), [src/servers/mcp-server.ts](https://github.com/thedotmack/claude-mem/blob/b6e05382e2be35e29f22335f04a6890a4c0ba976/src/servers/mcp-server.ts).
- Source 3: [DeusData/codebase-memory-mcp, pinned snapshot](https://github.com/DeusData/codebase-memory-mcp/tree/fe85a6b2360839eee98fc6316e0ea58d38f15d60); inspected: [src/mcp/compact_out.h](https://github.com/DeusData/codebase-memory-mcp/blob/fe85a6b2360839eee98fc6316e0ea58d38f15d60/src/mcp/compact_out.h), [src/mcp/compact_out.c](https://github.com/DeusData/codebase-memory-mcp/blob/fe85a6b2360839eee98fc6316e0ea58d38f15d60/src/mcp/compact_out.c).
- Source 4: [topoteretes/cognee, pinned snapshot](https://github.com/topoteretes/cognee/tree/78ff576559a7f75f65884c5bd90b22cdc790016e); inspected: [cognee/memify_pipelines/apply_feedback_weights.py](https://github.com/topoteretes/cognee/blob/78ff576559a7f75f65884c5bd90b22cdc790016e/cognee/memify_pipelines/apply_feedback_weights.py), [cognee/tasks/memify/apply_feedback_weights.py](https://github.com/topoteretes/cognee/blob/78ff576559a7f75f65884c5bd90b22cdc790016e/cognee/tasks/memify/apply_feedback_weights.py).
- Source 5: [garrytan/gbrain, pinned snapshot](https://github.com/garrytan/gbrain/tree/8c70f6255047a7647adb30b1d6333a48068d9fa5); inspected: [src/core/cycle/triage-rescue.ts](https://github.com/garrytan/gbrain/blob/8c70f6255047a7647adb30b1d6333a48068d9fa5/src/core/cycle/triage-rescue.ts), [src/core/cycle/synthesize-verify.ts](https://github.com/garrytan/gbrain/blob/8c70f6255047a7647adb30b1d6333a48068d9fa5/src/core/cycle/synthesize-verify.ts), [src/core/search/hybrid.ts](https://github.com/garrytan/gbrain/blob/8c70f6255047a7647adb30b1d6333a48068d9fa5/src/core/search/hybrid.ts), [test/cycle-triage-rescue.test.ts](https://github.com/garrytan/gbrain/blob/8c70f6255047a7647adb30b1d6333a48068d9fa5/test/cycle-triage-rescue.test.ts).
- Source 6: [abhigyanpatwari/GitNexus, pinned snapshot](https://github.com/abhigyanpatwari/GitNexus/tree/a049b2dac6433b3c13185e226483fa85743dab1e); inspected: [eval/workflow_bench/evolution.py](https://github.com/abhigyanpatwari/GitNexus/blob/a049b2dac6433b3c13185e226483fa85743dab1e/eval/workflow_bench/evolution.py), [LICENSE](https://github.com/abhigyanpatwari/GitNexus/blob/a049b2dac6433b3c13185e226483fa85743dab1e/LICENSE).
- Source 7: [Graphify-Labs/graphify, pinned snapshot](https://github.com/Graphify-Labs/graphify/tree/c9f99018774e2e0380e9f65b3959944559a0d5f6); inspected: [graphify/dedup.py](https://github.com/Graphify-Labs/graphify/blob/c9f99018774e2e0380e9f65b3959944559a0d5f6/graphify/dedup.py), [LICENSE](https://github.com/Graphify-Labs/graphify/blob/c9f99018774e2e0380e9f65b3959944559a0d5f6/LICENSE).
- Source 8: [getzep/graphiti, pinned snapshot](https://github.com/getzep/graphiti/tree/547422865cca9fb5a82915c074d899428c145ff4); inspected: [graphiti_core/search/search_config_recipes.py](https://github.com/getzep/graphiti/blob/547422865cca9fb5a82915c074d899428c145ff4/graphiti_core/search/search_config_recipes.py), [graphiti_core/search/search_filters.py](https://github.com/getzep/graphiti/blob/547422865cca9fb5a82915c074d899428c145ff4/graphiti_core/search/search_filters.py).
- Source 9: [letta-ai/letta, pinned snapshot](https://github.com/letta-ai/letta/tree/4511fa0bc91f68fbab32b91f694617271ea9012b); inspected: [README.md](https://github.com/letta-ai/letta/blob/4511fa0bc91f68fbab32b91f694617271ea9012b/README.md).
- Source 10: [letta-ai/letta-code, pinned snapshot](https://github.com/letta-ai/letta-code/tree/7a4337e215bc2fa7b1af541a8b249d56960de802); inspected: [README.md](https://github.com/letta-ai/letta-code/blob/7a4337e215bc2fa7b1af541a8b249d56960de802/README.md), [src/cli/helpers/post-turn-reflection.ts](https://github.com/letta-ai/letta-code/blob/7a4337e215bc2fa7b1af541a8b249d56960de802/src/cli/helpers/post-turn-reflection.ts).
- Source 11: [mem0ai/mem0, pinned snapshot](https://github.com/mem0ai/mem0/tree/dae67f74f5cc7bf138c7d7d6f9cec5ce4b4373b3); inspected: [mem0/memory/main.py](https://github.com/mem0ai/mem0/blob/dae67f74f5cc7bf138c7d7d6f9cec5ce4b4373b3/mem0/memory/main.py), [docs/api-reference/dream/dream-preview.mdx](https://github.com/mem0ai/mem0/blob/dae67f74f5cc7bf138c7d7d6f9cec5ce4b4373b3/docs/api-reference/dream/dream-preview.mdx), [docs/api-reference/dream/get-dream-memory-sources.mdx](https://github.com/mem0ai/mem0/blob/dae67f74f5cc7bf138c7d7d6f9cec5ce4b4373b3/docs/api-reference/dream/get-dream-memory-sources.mdx).
- Source 12: [MemPalace/mempalace, pinned snapshot](https://github.com/MemPalace/mempalace/tree/d9f059076c866fa6f29195679d75712436986024); inspected: [mempalace/mcp_light_server.py](https://github.com/MemPalace/mempalace/blob/d9f059076c866fa6f29195679d75712436986024/mempalace/mcp_light_server.py), [website/guide/lightweight-mcp.md](https://github.com/MemPalace/mempalace/blob/d9f059076c866fa6f29195679d75712436986024/website/guide/lightweight-mcp.md).
- Source 13: [gnekt/My-Brain-Is-Full-Crew, pinned snapshot](https://github.com/gnekt/My-Brain-Is-Full-Crew/tree/238ae8c45301cc1ad8ef338288720cee1f0ada93); inspected: [README.md](https://github.com/gnekt/My-Brain-Is-Full-Crew/blob/238ae8c45301cc1ad8ef338288720cee1f0ada93/README.md), [LICENSE](https://github.com/gnekt/My-Brain-Is-Full-Crew/blob/238ae8c45301cc1ad8ef338288720cee1f0ada93/LICENSE).
- Source 14: [volcengine/OpenViking, pinned snapshot](https://github.com/volcengine/OpenViking/tree/0c5147cae26aec8d6d93445ec6ad86d5faff4035); inspected: [openviking/retrieve/context_assembler/ledger.py](https://github.com/volcengine/OpenViking/blob/0c5147cae26aec8d6d93445ec6ad86d5faff4035/openviking/retrieve/context_assembler/ledger.py), [LICENSE](https://github.com/volcengine/OpenViking/blob/0c5147cae26aec8d6d93445ec6ad86d5faff4035/LICENSE).
- Source 15: [langchain-ai/openwiki, pinned snapshot](https://github.com/langchain-ai/openwiki/tree/1e6d54cdfeec334c29cf61800610193f33dc0d24); inspected: [src/claims/evidence/repository/resolver.ts](https://github.com/langchain-ai/openwiki/blob/1e6d54cdfeec334c29cf61800610193f33dc0d24/src/claims/evidence/repository/resolver.ts), [src/claims/brains/code/preflight.ts](https://github.com/langchain-ai/openwiki/blob/1e6d54cdfeec334c29cf61800610193f33dc0d24/src/claims/brains/code/preflight.ts), [src/claims/core/types.ts](https://github.com/langchain-ai/openwiki/blob/1e6d54cdfeec334c29cf61800610193f33dc0d24/src/claims/core/types.ts).
- Source 16: [oraios/serena, pinned snapshot](https://github.com/oraios/serena/tree/13ac8c5b1d51873bd148aea440dcb22f85d3a439); inspected: [src/serena/memories/memory_manager.py](https://github.com/oraios/serena/blob/13ac8c5b1d51873bd148aea440dcb22f85d3a439/src/serena/memories/memory_manager.py).
- Source 17: [getzep/zep, pinned snapshot](https://github.com/getzep/zep/tree/54f63eeb58dbc2f6ac5995cd4d51b56317cf5e42); inspected: [README.md](https://github.com/getzep/zep/blob/54f63eeb58dbc2f6ac5995cd4d51b56317cf5e42/README.md).

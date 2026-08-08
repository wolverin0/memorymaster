# MemoryMaster paper-radar review - 2026-08-08
# Covers: primary-paper findings, current-system gaps, and bounded implementation decisions for MemoryMaster.
# Key terms: paper radar, filesystem memory, BudgetMem, A2RAG, temporal memory, governed skills, Mem2ActBench.
# Read when: selecting research-derived work or checking why a memory technique was adopted, benchmarked, or rejected.
# Sources: VoltAgent radar commit c8502b6 plus 18 version-pinned arXiv PDFs; upstream summaries are discovery only.
# Verdict: adopt bounded capabilities, benchmark seven hypotheses, retain existing boundaries, reject wholesale rewrites.
# Updated: 2026-08-08 after metadata triage, primary-PDF extraction, result/limitation review, and figure inspection.

## Executive verdict

The papers do not justify replacing MemoryMaster. They reinforce its strongest
choices: authoritative governed claims, preserved evidence, explicit scope,
lifecycle state, human promotion, and graph results rehydrated through claims.
They also expose five useful gaps:

1. LongMemEval retrieval and QA do not prove that an agent can apply memory to
   a tool action without hallucinating missing parameters.
2. Graph recall rehydrates claims, but it lacks a bounded fallback that maps a
   graph/claim signal back to exact evidence excerpts when extraction omitted a
   qualifier.
3. `event_time`, validity intervals, and supersession exist, but temporal recall
   still ranks mostly by freshness instead of query time, occurrence time, and
   durative state.
4. `personal-skill-v1` has governance, versions, validation, and citations, but
   not explicit execution outcomes or failure-derived warnings.
5. Token budgets exist, but cost is not attributed consistently to retrieval,
   extraction, graph expansion, evidence admission, skill review, and answer
   generation stages.

The first implementation wave must add evaluation and telemetry before changing
retrieval. No paper-derived behavior becomes a default without a reproducible
quality or cost win on MemoryMaster's authoritative path.

## Review scope and evidence standard

- Discovery feed: `VoltAgent/awesome-ai-agent-papers` at commit
  `c8502b6acd3978a84b8b25453eda24be83088d00`.
- The pinned Memory & RAG section contains 57 parseable paper records although
  its heading says 56 and its table of contents says 57. Parser output, not the
  displayed count, is authoritative for the radar snapshot.
- All 57 Memory & RAG records received metadata and abstract-level triage using
  primary arXiv metadata.
- Fifteen high-relevance Memory & RAG PDFs, the earlier filesystem-memory PDF,
  and two cross-section evaluation/cost PDFs received a method, result,
  ablation, limitation, and applicability review.
- Key pages from the filesystem, BudgetMem, Skill-Pro, A2RAG, temporal-memory,
  and Mem2ActBench papers were rendered and visually inspected to verify tables
  and diagrams rather than relying only on extracted text.
- Upstream descriptions and paper claims are not accepted as facts about
  MemoryMaster. Every proposal below is compared with current code and must be
  tested locally.

## Current MemoryMaster baseline

| Surface | Already present | Research-exposed gap |
|---|---|---|
| Retrieval | Explicit profiles, query classification, score explanations, token-bounded packing | Profiles mostly change weights; no measured multi-stage evidence sufficiency or admission policy |
| Scope | SQLite and graph queries filter authorized scopes before trusted results are returned | Add invariant tests proving every future routing stage masks unauthorized data before scoring or provider calls |
| Evidence | Source -> evidence -> claim lineage with content hashes and citations | No query-time exact-evidence map-back when a claim/edge lacks a required qualifier |
| Graph | Supported edges, confirmed active claim authority, scope filtering, replay-safe support, claim rehydration | No progressive local -> path -> evidence fallback with explicit sufficiency diagnostics |
| Temporal | `event_time`, `valid_from`, `valid_until`, supersession, freshness profile | No occurrence-time intent matching, interval overlap, or durative-state projection |
| Skills | Strict `personal-skill-v1`, activation cues, workflow, validation, immutable versions, citations, human promotion | No success/failure/ambiguous execution evidence, termination condition, or negative warning path |
| Evaluation | LongMemEval retrieval/QA, capture/graph quality, latency, replay and scope gates | No active tool-use benchmark, preservation score, or per-stage cost attribution |

## Paper decisions

| Paper | Verdict | What MemoryMaster should take | What it should not take |
|---|---|---|---|
| [Filesystem-Based Memory for LLM Agents](https://arxiv.org/abs/2607.26637v1) | **Adopt evaluation** | Measure answer quality, preservation, store health, consumer strength, and total retrieval cost separately | Filesystem authority, autonomous reorganization, or organization as a quality proxy |
| [BudgetMem](https://arxiv.org/abs/2602.06025v1) | **Benchmark** | Explicit low/mid/high query budgets and stage-level cost/quality frontiers | An RL router or multiple model tiers before deterministic policies beat the baseline |
| [Skill-Pro / ProcMEM](https://arxiv.org/abs/2602.01869v1) | **Adopt bounded schema ideas** | Activation, execution, termination, outcome evidence, and validation before reuse | Autonomous PPO evolution, score-based deletion, or automatic promotion |
| [E-mem](https://arxiv.org/abs/2601.21714v1) | **Benchmark** | Bounded reconstruction from preserved contiguous evidence for multi-hop/narrative queries | Multiple resident memory agents or unbounded uncompressed contexts |
| [ShardMemo](https://arxiv.org/abs/2601.21545v1) | **Retain and harden** | Scope-before-routing, cheap-first tiers, versioned skills, safe fallback to evidence | Learned MoE sharding for a personal SQLite corpus without measured scale pressure |
| [A2RAG](https://arxiv.org/abs/2601.21162v1) | **Adopt** | Progressive local/path expansion, evidence-sufficiency diagnostics, and exact provenance map-back | Graph-only answers, unrestricted retry loops, or PPR before simpler traversal is measured |
| [Less is More for RAG](https://arxiv.org/abs/2601.17532v1) | **Benchmark** | Admission control, redundancy/conflict pruning, pass-rate and drift telemetry | Treating uncertainty reduction as truth or adding an LLM probe to every recall by default |
| [Grounding Agent Memory in Contextual Intent](https://arxiv.org/abs/2601.10702v1) | **Benchmark** | Explicit goal/action/entity cues and coarse episode boundaries for opt-in task retrieval | Multiple ingestion LLM calls per turn or uncontrolled label evolution |
| [Beyond Dialogue Time](https://arxiv.org/abs/2601.07468v1) | **Adopt** | Occurrence-time retrieval, interval overlap, and a derived durative-state projection | Rewriting atomic evidence into a new authority or fixed monthly granularity |
| [Reliable Graph-RAG for Codebases](https://arxiv.org/abs/2601.08773v1) | **Retain boundary** | Prefer deterministic structural providers and measure indexing coverage | A second MemoryMaster code graph; GitNexus remains the code-topology specialist |
| [Seeing through the Conflict](https://arxiv.org/abs/2601.06842v1) | **Adopt observability only** | Separate semantic relevance, evidence consistency, and answer sufficiency in diagnostics | Trusting model-parametric memory over confirmed evidence or learned soft prompts |
| [Amory](https://arxiv.org/abs/2601.06282v1) | **Benchmark** | Narrative-arc co-retrieval from preserved evidence | Autonomous narrative rewriting or synthetic-conversation conclusions as production proof |
| [Controllable Memory Usage](https://arxiv.org/abs/2601.05107v1) | **Benchmark later** | An explicit caller-selected memory-reliance profile for fresh-start versus continuity tasks | Silent inference of how much history to obey or model fine-tuning for the first version |
| [Proactive Memory Extraction](https://arxiv.org/abs/2601.04463v1) | **Benchmark** | Targeted re-extraction when a query exposes missing evidence; track integrity separately from accuracy | Repeated self-questioning on every capture or automatic replacement of preserved evidence |
| [Membox](https://arxiv.org/abs/2601.03785v2) | **Adopt deterministic subset** | Retrieve adjacent evidence spans and link recurring source episodes without rewriting them | LLM-created topic boxes as authoritative memory |
| [MAGMA](https://arxiv.org/abs/2601.03236v1) | **Defer architecture** | Compare temporal, causal, and entity path signals inside the supported graph | Parallel semantic/temporal/causal graph authorities or policy-learned traversal |
| [Mem2ActBench](https://arxiv.org/abs/2601.19935v1) | **Adopt benchmark shape** | Score retrieval miss, retrieved-but-unused, hallucinated default, lossless-retention failure, tool error, and exact argument grounding | Treat its synthetic offline tool calls as sufficient production proof |
| [Tokenomics](https://arxiv.org/abs/2601.14470v1) | **Adopt telemetry** | Attribute input/output/reasoning tokens and latency by MemoryMaster stage | Generalize its 30-task, one-framework results as MemoryMaster's expected distribution |

## Ordered implementation packages

### PPR-1 - Representation and active-use evaluation

Add a versioned evaluation set and harness before product behavior changes:

- latest versus superseded state;
- occurrence time versus dialogue time;
- valid interval and durative-state questions;
- affect/emphasis preservation;
- narrative-arc co-retrieval;
- active tool invocation with exact parameters;
- missing/default/inferred parameter distinctions;
- retrieval miss, retrieved-but-unused, hallucinated default, lossless-retention
  failure, and wrong-tool attribution;
- answer correctness and citation correctness scored independently.

Run the matrix against claims-only, evidence-only, claims+evidence,
claims+approved-skills, and claims+ephemeral-guidance. Retain the existing
LongMemEval R@5/MRR and full-QA regression gates.

### PPR-2 - Stage-level sustainability telemetry

Emit bounded per-request stage observations for retrieval, graph expansion,
evidence map-back, admission, packing, skill recall/review, and answer/judge
generation. Record elapsed time, provider calls, content read, input/output
tokens when available, cache state, selected tier, fallback reason, and final
correctness in evaluation artifacts. Do not persist private query or evidence
text in aggregate telemetry.

### PPR-3 - Deterministic budget and admission policy

Introduce an explicit versioned policy selected by the caller:

- `low`: lexical/confirmed claims, small evidence budget, no provider call;
- `balanced`: current governed recall plus bounded graph/evidence fallback;
- `high`: larger candidate window and explicit evidence-sufficiency check;
- `temporal`: lifecycle timeline, occurrence-time/interval matching, evidence;
- `procedural`: confirmed skills, warnings, and supporting claims.

Start with deterministic rules and shadow evaluation. Add redundancy,
near-duplicate, lifecycle-conflict, and weak-support admission diagnostics before
testing any generator-aligned LLM pruning. Scope and sensitivity filtering must
occur before tier selection, scoring, provider access, or cache lookup.

### PPR-4 - Progressive claim-to-evidence rehydration

Treat graph/entity matches only as navigation signals:

1. retrieve authorized confirmed claims;
2. expand one bounded supported path when the query is relational;
3. test whether required entities, relations, temporal qualifiers, and citations
   are present;
4. map selected claims through `claim_evidence_links` to exact evidence excerpts;
5. return a diagnostic fallback reason when evidence remains insufficient.

No graph-generated fact may bypass claim status, scope, sensitivity, retired
source, or citation checks.

### PPR-5 - Temporal and episode projections

Add derived, rebuildable projections over authoritative claims/evidence:

- query-time versus occurrence-time intent;
- interval overlap using `valid_from`/`valid_until`;
- explicit latest/current versus historical selection;
- bounded adjacent evidence windows from source order;
- recurring episode links derived from stable source/session metadata;
- durative state summaries that cite every contributing claim and never replace
  atomic history.

This package needs temporal precision and supersession adversarial tests before
any schema or ranking change.

### PPR-6 - Outcome-aware governed skills

Extend skill evidence additively with execution observations:

- outcome: `success`, `failure`, or `ambiguous`;
- consumer/model profile and tool/schema snapshot;
- activation match, termination result, validation result, and bounded metrics;
- failure-derived warnings kept separate from positive procedures.

Success may strengthen a review signal; failure must not strengthen a positive
skill. No outcome automatically confirms, rewrites, prunes, or archives a skill.
The steward and operator remain the only promotion authority.

## Remaining radar triage

The other 42 Memory & RAG papers remain in the radar, not discarded:

- training-heavy routers, reinforcement-learning retrieval, and learned memory
  controllers are deferred until deterministic policies have a measured ceiling;
- domain-specific financial, supply-chain, scientific, SOP, embodied, multimodal,
  and text-table systems are reference material unless MemoryMaster acquires the
  corresponding use case;
- surveys inform terminology but cannot establish an implementation gain;
- multi-agent memory managers, autonomous compaction, learned forgetting, and
  self-rewriting memory require adversarial preservation and governance evidence;
- alternative GraphRAG systems remain benchmarks; they do not create another
  authoritative graph or answer path.

The next radar refresh should diff the pinned revision, classify only new or
changed entries, and choose the next full-PDF batch from untested gaps rather
than repeatedly rereading similar architectures.

## Non-adoptions fixed by this review

- No filesystem or Obsidian authority.
- No automatic deletion, compression, consolidation, or trusted promotion.
- No autonomous PPO/RL router in the personal SQLite profile.
- No graph-only or vector-only answer authority.
- No bulk paper-PDF ingestion into governed memory.
- No full-context or multi-agent memory process as a default retrieval path.
- No claimed improvement from paper-reported metrics without a MemoryMaster
  baseline, mutation-relevant comparison, and reproducible local result.

## Exit evidence required

A research-derived package is complete only when its artifact records:

- the exact dataset and provider/model identity;
- the unchanged baseline and the candidate run;
- quality, preservation, citation, cost, latency, scope, and replay results;
- a wiring-relevant negative control or mutation where applicable;
- explicit `adopt`, `retain`, `defer`, or `reject` disposition;
- code and rollback scope;
- zero secret and cross-scope leakage;
- no regression beyond the existing LongMemEval and full-QA thresholds.

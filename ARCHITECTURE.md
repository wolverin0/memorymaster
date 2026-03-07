# Memory Reliability System (v1) - Architecture

## 1) Purpose and Scope
This document defines a v1 memory reliability system for coding agents. The system is designed to improve factual persistence across sessions while preventing drift, stale assumptions, and unsafe disclosure.

### Primary objectives
- Persist useful agent knowledge as verifiable claims.
- Track claim confidence and lifecycle state over time.
- Continuously validate and demote invalid memories.
- Retrieve high-precision memories with provenance.
- Compact long histories into summaries without losing citation traceability.

### Non-goals (v1)
- Cross-organization multi-tenant isolation in one deployment.
- Autonomous policy updates without human approval.
- Fully automatic conflict resolution for high-impact claims.

## 2) Architecture Overview
The system has six major subsystems:

1. Event Log (append-only source of truth).
2. Structured Claims Store (normalized, queryable memory facts).
3. Lifecycle State Engine (state transitions and invariants).
4. Validator Loop (continuous re-check and state correction).
5. Retrieval Stack (query understanding, ranking, filtering, assembly).
6. Compaction Engine (history summarization with citations).

```text
Agent Runtime
  -> Event Ingestor -> Event Log (append-only)
                      -> Claim Extractor -> Claims Store
                      -> State Engine
Validator Scheduler -> Validator Workers -> State Engine + Claims Store
Query Path -> Retrieval Stack -> Response Context Builder
Compactor -> Summaries + Citation Graph -> Claims Store/Archive
Policy Guardrails -> Security Controls (applied at ingest/retrieval/export)
```

## 3) Event Log
The event log is immutable and append-only.

### Event types (v1)
- `interaction`: user or agent turn.
- `observation`: external tool result or system observation.
- `claim_created`: new structured claim extracted.
- `claim_updated`: claim metadata/state/confidence changed.
- `validation_result`: validator success/failure/inconclusive.
- `compaction_run`: compaction output and retained citations.
- `policy_decision`: allow/deny/redact action.

### Required event fields
- `event_id` (ULID/UUIDv7).
- `timestamp_utc` (RFC3339).
- `actor` (`user`, `agent`, `system`, `validator`).
- `session_id`, `thread_id`, `workspace_id`.
- `event_type`.
- `payload` (schema per event type).
- `integrity_hash` (content hash chained to previous event hash per stream).

### Reliability properties
- Idempotent writes via deterministic `event_id` for retries.
- Strict ordering within partition (`workspace_id` + `thread_id`).
- At-least-once delivery to downstream consumers.

## 4) Structured Claims Model
Claims are atomic, machine-checkable memory units extracted from events.

### Claim schema (v1)
- `claim_id`
- `subject` (entity being described)
- `predicate` (relation/property)
- `object` (value/entity)
- `claim_text` (human-readable paraphrase)
- `source_event_ids[]` (direct citations)
- `source_spans[]` (optional offsets/line ranges)
- `confidence` (`0.0-1.0`)
- `state` (`candidate|confirmed|stale|superseded|conflicted|archived`)
- `valid_from`, `valid_to` (nullable temporal bounds)
- `last_validated_at`
- `validation_policy` (rule id / validator class)
- `sensitivity` (`public|internal|secret`)
- `created_at`, `updated_at`

### Citation requirements
- Every claim must cite at least one source event.
- Derived/compacted claims must preserve transitive citation links to originals.
- Retrieval output must include citations for all non-trivial factual assertions.

## 5) Lifecycle States and Transitions
States represent reliability and recency, not just existence.

### State semantics
- `candidate`: newly extracted, unverified.
- `confirmed`: validated by at least one rule or repeated corroboration.
- `stale`: likely outdated by time horizon or failed freshness checks.
- `superseded`: replaced by newer conflicting claim with stronger evidence.
- `conflicted`: unresolved contradiction among similarly strong claims.
- `archived`: retained for audit/history, excluded from default retrieval.

### Transition rules (core)
- `candidate -> confirmed`: validator success or N corroborating sources.
- `candidate -> conflicted`: contradiction detected before confirmation.
- `confirmed -> stale`: freshness TTL exceeded or soft validation failure.
- `confirmed -> superseded`: newer claim validated for same key tuple.
- `any active -> conflicted`: high-confidence contradiction appears.
- `stale|superseded|conflicted -> archived`: retention/compaction policy.
- `stale -> confirmed`: revalidation success.

### Invariants
- Exactly one active `confirmed` claim per uniqueness key (`subject`, `predicate`, scope) unless relation is multi-valued.
- `superseded` claims must reference successor `claim_id`.
- `archived` claims are immutable except retention metadata.

## 6) Validator Loop
Validators continuously test claim correctness and freshness.

### Loop design
1. Scheduler picks due claims by priority queue.
2. Worker runs validator class based on `validation_policy`.
3. Result recorded as `validation_result` event.
4. State engine applies transition and updates confidence.
5. Backoff and re-queue according to outcome.

### Validator classes (v1)
- `consistency`: internal contradiction checks.
- `freshness`: TTL and temporal relevance checks.
- `source_replay`: re-read cited source events for drift.
- `external_probe` (optional): tool/API checks for volatile facts.

### Outcome contract
- `pass`: increase confidence, possibly promote state.
- `fail_soft`: reduce confidence, often mark `stale`.
- `fail_hard`: mark `conflicted` or `superseded` when replacement exists.
- `inconclusive`: no state promotion, shorter recheck interval.

## 7) Retrieval Stack
Retrieval prioritizes trustworthy, recent, and relevant claims.

### Stages
1. **Query parsing**: detect entities, intent, temporal constraints.
2. **Candidate generation**:
   - lexical/embedding search over claims + compacted summaries,
   - graph expansion via linked claims/citations.
3. **Policy filter**: sensitivity and workspace access checks.
4. **State filter**:
   - default include `confirmed`,
   - conditional include `candidate` when uncertainty is acceptable,
   - exclude `archived` by default.
5. **Ranking**:
   - relevance score,
   - state weight,
   - confidence,
   - recency/validity window.
6. **Context assembly**:
   - top-k claims,
   - citation bundle,
   - conflict notes if `conflicted` claims exist.

### Retrieval behavior rules
- Never present `candidate` as definitive fact.
- If conflict exists, output explicit uncertainty and both citations.
- If only stale evidence exists, annotate with freshness warning.

## 8) Compaction with Citations
Compaction reduces storage and retrieval cost while preserving verifiability.

### Inputs
- old event segments,
- low-access claims,
- stale/superseded claim groups.

### Outputs
- compacted summary objects,
- citation graph mapping summary assertions -> original events/claims,
- archive markers for pruned active records.

### Safety constraints
- No citation, no compaction.
- Compaction must be reversible to source references.
- Preserve contradictory evidence; do not collapse unresolved conflicts.

## 9) Security Controls
Security is enforced at ingest, storage, retrieval, and export boundaries.

### Controls (v1)
- Encryption at rest and in transit.
- Access control by workspace/session identity.
- Claim-level sensitivity tags and retrieval redaction.
- Audit events for all policy decisions and privileged reads.
- Secret detection on ingestion with automatic masking.
- Data retention and deletion controls by policy class.
- Immutable audit trail for claim state transitions.

### Threats addressed
- Unauthorized retrieval of sensitive memory.
- Prompt-induced exfiltration of hidden claims.
- Tampering with historical events or citations.
- Silent drift from unvalidated stale memories.

## 10) Evaluation Metrics
Metrics are required for reliability sign-off.

### Quality metrics
- `Claim Precision@state=confirmed`: fraction of confirmed claims judged correct.
- `Claim Recall@critical`: coverage of critical facts in benchmark tasks.
- `Conflict Detection Rate`: detected contradictions / total injected contradictions.
- `Staleness Catch Rate`: stale claims detected before use.
- `Citation Completeness`: assertions with valid citations / total assertions.

### System metrics
- Validation throughput (claims/hour).
- Validation latency p50/p95.
- Retrieval latency p50/p95.
- Compaction ratio (raw events to summarized units).
- Storage growth per 1k interactions.

### Safety metrics
- Sensitive claim leakage rate.
- Unauthorized access denial effectiveness.
- Audit log completeness.

## 11) v1 Deployment Topology (Reference)
- `memory-api`: ingest/retrieval endpoints.
- `event-log`: append-only store.
- `claim-db`: relational/document index for structured claims.
- `validator-workers`: async workers.
- `retrieval-service`: ranking and context assembly.
- `compactor-worker`: scheduled compaction.
- `policy-service`: authorization + redaction decisions.

## 12) Definition of Done for v1 Architecture
- End-to-end flow from ingest -> claim extraction -> validation -> retrieval works in staging.
- Lifecycle transitions are deterministic and audited.
- All retrieval outputs include citations for factual claims.
- Security controls enforce sensitivity boundaries.
- Metrics dashboard tracks reliability, performance, and safety KPIs.

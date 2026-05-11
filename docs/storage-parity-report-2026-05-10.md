# Storage Parity Report - 2026-05-10

SQLite source of truth: `memorymaster.storage.SQLiteStore`, including public methods inherited from its storage mixins.
Postgres target: `memorymaster.postgres_store.PostgresStore`.

Rule file read: `.claude/rules/storage-parity.md`. Relevant constraints: schemas and storage adapters must stay in sync, SQLite WAL mode is mandatory, schema changes must go through schema files/migrations, and SQLite-only tests should get Postgres counterparts.

## Summary

- Public SQLite methods audited: 59
- Public Postgres methods visible after fixes: 59
- Drift items found before this change: 16
- Safe delegation fixes implemented: 3
- Remaining drift items after this change: 13

## Gap Classification

### Fixed In This Change

- `list_citations_batch`: missing/direct behavior drift: inherited SQLite placeholder implementation; fixed with PostgresStore wrapper delegating to list_citations.
- `count_citations_batch`: missing/direct behavior drift: inherited SQLite placeholder implementation; fixed with PostgresStore wrapper delegating to count_citations.
- `set_normalized_texts_batch`: missing/direct behavior drift: inherited SQLite placeholder implementation; fixed with PostgresStore wrapper delegating to set_normalized_text.

### Missing Direct Postgres Equivalent / Behavior Drift Remaining

- `query_as_of`: missing/direct behavior drift: still inherited from SQLite _ReadMixin; uses SQLite SQL placeholders/connection assumptions.
- `recompute_tiers`: missing/direct behavior drift: still inherited from SQLite _LifecycleMixin; writes with SQLite SQL assumptions.
- `record_access`: missing/direct behavior drift: still inherited from SQLite _LifecycleMixin; writes with SQLite SQL placeholders.
- `record_accesses_batch`: missing/direct behavior drift: still inherited from SQLite _LifecycleMixin; writes with SQLite SQL placeholders.
- `traverse_relationships`: missing/direct behavior drift: still inherited from SQLite _ReadMixin; uses SQLite recursive traversal SQL assumptions.

### Signature Drift Remaining

- `connect`: signature drift only: return annotation differs; behavior is backend-specific by design.
- `create_claim`: signature and behavior drift: PostgresStore lacks event_time, valid_from, valid_until, source_agent, and visibility parameters.
- `add_evidence_item`: signature annotation drift: payload_json uses Any on SQLite and object on Postgres.
- `create_action_proposal`: signature annotation drift: payload_json uses Any on SQLite and object on Postgres.
- `update_action_proposal_fields`: signature annotation drift: payload_json uses Any on SQLite and object on Postgres.
- `update_action_proposal_status`: signature annotation drift: payload_json uses Any on SQLite and object on Postgres.
- `upsert_external_source`: signature annotation drift: config_json uses Any on SQLite and object on Postgres.
- `upsert_source_item`: signature annotation drift: payload_json uses Any on SQLite and object on Postgres.

## Recommended Fixes

1. Add direct Postgres implementations for `record_access` and `record_accesses_batch`; these affect query feedback/tiering and should use Postgres `%s` placeholders plus a single transaction.
2. Add direct Postgres implementations for `query_as_of` and `traverse_relationships`; these are read paths currently inherited from SQLite.
3. Align `create_claim` with SQLite's temporal/source/visibility parameters before relying on Postgres for bi-temporal ingestion.
4. Normalize the `Any` versus `object` annotation drift in source/action proposal APIs, or document why the public signatures intentionally differ.

## Parity Matrix

| Method | SQLite signature | Postgres signature | Signature match? | Postgres owner |
|---|---|---|---|---|
| `add_claim_link` | `(self, source_id: 'int', target_id: 'int', link_type: 'str') -> 'ClaimLink'` | `(self, source_id: 'int', target_id: 'int', link_type: 'str') -> 'ClaimLink'` | yes | direct |
| `add_evidence_item` | `(self, *, source_item_id: 'int', evidence_type: 'str', text: 'str | None' = None, media_path: 'str | None' = None, provider: 'str | None' = None, confidence: 'float | None' = None, payload_json: 'dict[str, Any] | str | None' = None, sensitivity: 'str | None' = None) -> 'EvidenceItem'` | `(self, *, source_item_id: 'int', evidence_type: 'str', text: 'str | None' = None, media_path: 'str | None' = None, provider: 'str | None' = None, confidence: 'float | None' = None, payload_json: 'dict[str, object] | str | None' = None, sensitivity: 'str | None' = None) -> 'EvidenceItem'` | no | direct |
| `apply_status_transition` | `(self, claim: 'Claim', *, to_status: 'str', reason: 'str', event_type: 'str', replaced_by_claim_id: 'int | None' = None) -> 'Claim'` | `(self, claim: 'Claim', *, to_status: 'str', reason: 'str', event_type: 'str', replaced_by_claim_id: 'int | None' = None) -> 'Claim'` | yes | direct |
| `claim_pending_media_retries` | `(self, limit: 'int' = 25) -> 'list[MediaRetryItem]'` | `(self, limit: 'int' = 25) -> 'list[MediaRetryItem]'` | yes | direct |
| `connect` | `(self) -> 'sqlite3.Connection'` | `(self)` | no | direct |
| `count_citations` | `(self, claim_id: 'int') -> 'int'` | `(self, claim_id: 'int') -> 'int'` | yes | direct |
| `count_citations_batch` | `(self, claim_ids: 'list[int]') -> 'dict[int, int]'` | `(self, claim_ids: 'list[int]') -> 'dict[int, int]'` | yes | direct |
| `create_action_proposal` | `(self, *, proposal_type: 'str', title: 'str', description: 'str | None' = None, source_item_id: 'int | None' = None, evidence_item_id: 'int | None' = None, claim_id: 'int | None' = None, suggested_due_at: 'str | None' = None, destination: 'str' = 'manual', confidence: 'float' = 0.5, payload_json: 'dict[str, Any] | str | None' = None, idempotency_key: 'str | None' = None) -> 'ActionProposal'` | `(self, *, proposal_type: 'str', title: 'str', description: 'str | None' = None, source_item_id: 'int | None' = None, evidence_item_id: 'int | None' = None, claim_id: 'int | None' = None, suggested_due_at: 'str | None' = None, destination: 'str' = 'manual', confidence: 'float' = 0.5, payload_json: 'dict[str, object] | str | None' = None, idempotency_key: 'str | None' = None) -> 'ActionProposal'` | no | direct |
| `create_claim` | `(self, text: 'str', citations: 'list[CitationInput]', *, idempotency_key: 'str | None' = None, claim_type: 'str | None' = None, subject: 'str | None' = None, predicate: 'str | None' = None, object_value: 'str | None' = None, scope: 'str' = 'project', volatility: 'str' = 'medium', confidence: 'float' = 0.5, tenant_id: 'str | None' = None, event_time: 'str | None' = None, valid_from: 'str | None' = None, valid_until: 'str | None' = None, source_agent: 'str | None' = None, visibility: 'str' = 'public') -> 'Claim'` | `(self, text: 'str', citations: 'list[CitationInput]', *, idempotency_key: 'str | None' = None, claim_type: 'str | None' = None, subject: 'str | None' = None, predicate: 'str | None' = None, object_value: 'str | None' = None, scope: 'str' = 'project', volatility: 'str' = 'medium', confidence: 'float' = 0.5, tenant_id: 'str | None' = None) -> 'Claim'` | no | direct |
| `delete_old_events` | `(self, retain_days: 'int') -> 'int'` | `(self, retain_days: 'int') -> 'int'` | yes | direct |
| `enqueue_media_retry` | `(self, *, source_item_id: 'int', media_key: 'str', chat_id: 'str | None' = None, media_type: 'str | None' = None, media_path: 'str | None' = None, media_url: 'str | None' = None, status: 'str' = 'pending', next_attempt_time: 'str | None' = None) -> 'MediaRetryItem'` | `(self, *, source_item_id: 'int', media_key: 'str', chat_id: 'str | None' = None, media_type: 'str | None' = None, media_path: 'str | None' = None, media_url: 'str | None' = None, status: 'str' = 'pending', next_attempt_time: 'str | None' = None) -> 'MediaRetryItem'` | yes | direct |
| `find_by_status` | `(self, status: 'str', limit: 'int' = 100, include_citations: 'bool' = False) -> 'list[Claim]'` | `(self, status: 'str', limit: 'int' = 100, include_citations: 'bool' = False) -> 'list[Claim]'` | yes | direct |
| `find_confirmed_by_tuple` | `(self, *, subject: 'str | None', predicate: 'str | None', scope: 'str | None', exclude_claim_id: 'int | None' = None) -> 'list[Claim]'` | `(self, *, subject: 'str | None', predicate: 'str | None', scope: 'str | None', exclude_claim_id: 'int | None' = None) -> 'list[Claim]'` | yes | direct |
| `find_for_compaction` | `(self, retain_days: 'int', limit: 'int' = 500) -> 'list[Claim]'` | `(self, retain_days: 'int', limit: 'int' = 500) -> 'list[Claim]'` | yes | direct |
| `find_for_decay` | `(self, limit: 'int' = 200) -> 'list[Claim]'` | `(self, limit: 'int' = 200) -> 'list[Claim]'` | yes | direct |
| `get_action_proposal_by_idempotency_key` | `(self, idempotency_key: 'str') -> 'ActionProposal | None'` | `(self, idempotency_key: 'str') -> 'ActionProposal | None'` | yes | direct |
| `get_claim` | `(self, claim_id: 'int', include_citations: 'bool' = True) -> 'Claim | None'` | `(self, claim_id: 'int', include_citations: 'bool' = True) -> 'Claim | None'` | yes | direct |
| `get_claim_by_human_id` | `(self, human_id: 'str', include_citations: 'bool' = True) -> 'Claim | None'` | `(self, human_id: 'str', include_citations: 'bool' = True) -> 'Claim | None'` | yes | direct |
| `get_claim_by_idempotency_key` | `(self, idempotency_key: 'str', include_citations: 'bool' = True) -> 'Claim | None'` | `(self, idempotency_key: 'str', include_citations: 'bool' = True) -> 'Claim | None'` | yes | direct |
| `get_claim_links` | `(self, claim_id: 'int') -> 'list[ClaimLink]'` | `(self, claim_id: 'int') -> 'list[ClaimLink]'` | yes | direct |
| `get_derived_from_target_ids` | `(self, candidate_ids: 'list[int]') -> 'set[int]'` | `(self, candidate_ids: 'list[int]') -> 'set[int]'` | yes | direct |
| `get_linked_claims` | `(self, claim_id: 'int', link_type: 'str | None' = None) -> 'list[ClaimLink]'` | `(self, claim_id: 'int', link_type: 'str | None' = None) -> 'list[ClaimLink]'` | yes | direct |
| `get_source_item` | `(self, *, source_id: 'int', source_item_id: 'str') -> 'SourceItem | None'` | `(self, *, source_id: 'int', source_item_id: 'str') -> 'SourceItem | None'` | yes | direct |
| `get_source_item_by_id` | `(self, source_item_row_id: 'int') -> 'SourceItem | None'` | `(self, source_item_row_id: 'int') -> 'SourceItem | None'` | yes | direct |
| `init_db` | `(self) -> 'None'` | `(self) -> 'None'` | yes | direct |
| `list_action_proposals` | `(self, *, status: 'str | None' = None, destination: 'str | None' = None, limit: 'int' = 100) -> 'list[ActionProposal]'` | `(self, *, status: 'str | None' = None, destination: 'str | None' = None, limit: 'int' = 100) -> 'list[ActionProposal]'` | yes | direct |
| `list_citations` | `(self, claim_id: 'int') -> 'list[Citation]'` | `(self, claim_id: 'int') -> 'list[Citation]'` | yes | direct |
| `list_citations_batch` | `(self, claim_ids: 'list[int]') -> 'dict[int, list[Citation]]'` | `(self, claim_ids: 'list[int]') -> 'dict[int, list[Citation]]'` | yes | direct |
| `list_claims` | `(self, *, status: 'str | None' = None, status_in: 'list[str] | None' = None, limit: 'int' = 50, include_archived: 'bool' = False, text_query: 'str | None' = None, include_citations: 'bool' = False, scope_allowlist: 'list[str] | None' = None, tenant_id: 'str | None' = None) -> 'list[Claim]'` | `(self, *, status: 'str | None' = None, status_in: 'list[str] | None' = None, limit: 'int' = 50, include_archived: 'bool' = False, text_query: 'str | None' = None, include_citations: 'bool' = False, scope_allowlist: 'list[str] | None' = None, tenant_id: 'str | None' = None) -> 'list[Claim]'` | yes | direct |
| `list_events` | `(self, claim_id: 'int | None' = None, limit: 'int' = 100, event_type: 'str | None' = None) -> 'list[Event]'` | `(self, claim_id: 'int | None' = None, limit: 'int' = 100, event_type: 'str | None' = None) -> 'list[Event]'` | yes | direct |
| `list_evidence_items` | `(self, *, source_item_id: 'int | None' = None, evidence_type: 'str | None' = None, limit: 'int' = 100) -> 'list[EvidenceItem]'` | `(self, *, source_item_id: 'int | None' = None, evidence_type: 'str | None' = None, limit: 'int' = 100) -> 'list[EvidenceItem]'` | yes | direct |
| `list_media_retries` | `(self, *, status: 'str | None' = None, source_item_id: 'int | None' = None, limit: 'int' = 100) -> 'list[MediaRetryItem]'` | `(self, *, status: 'str | None' = None, source_item_id: 'int | None' = None, limit: 'int' = 100) -> 'list[MediaRetryItem]'` | yes | direct |
| `mark_superseded` | `(self, old_claim_id: 'int', new_claim_id: 'int', reason: 'str') -> 'None'` | `(self, old_claim_id: 'int', new_claim_id: 'int', reason: 'str') -> 'None'` | yes | direct |
| `media_retry_status_counts` | `(self) -> 'dict[str, int]'` | `(self) -> 'dict[str, int]'` | yes | direct |
| `query_as_of` | `(self, timestamp: 'str', *, limit: 'int' = 50) -> 'list[Claim]'` | `(self, timestamp: 'str', *, limit: 'int' = 50) -> 'list[Claim]'` | yes | inherited:_ReadMixin |
| `recompute_tiers` | `(self) -> 'dict[str, int]'` | `(self) -> 'dict[str, int]'` | yes | inherited:_LifecycleMixin |
| `reconcile_integrity` | `(self, *, fix: 'bool' = False, limit: 'int' = 500) -> 'dict[str, object]'` | `(self, *, fix: 'bool' = False, limit: 'int' = 500) -> 'dict[str, object]'` | yes | direct |
| `record_access` | `(self, claim_id: 'int') -> 'None'` | `(self, claim_id: 'int') -> 'None'` | yes | inherited:_LifecycleMixin |
| `record_accesses_batch` | `(self, claim_ids: 'list[int]') -> 'None'` | `(self, claim_ids: 'list[int]') -> 'None'` | yes | inherited:_LifecycleMixin |
| `record_event` | `(self, *, claim_id: 'int | None', event_type: 'str', from_status: 'str | None' = None, to_status: 'str | None' = None, details: 'str | None' = None, payload: 'dict[str, object] | None' = None) -> 'None'` | `(self, *, claim_id: 'int | None', event_type: 'str', from_status: 'str | None' = None, to_status: 'str | None' = None, details: 'str | None' = None, payload: 'dict[str, object] | None' = None) -> 'None'` | yes | direct |
| `record_media_retry_outcome` | `(self, retry_id: 'int', *, status: 'str', media_path: 'str | None' = None, last_http_status: 'int | None' = None, last_error: 'str | None' = None, next_attempt_time: 'str | None' = None) -> 'MediaRetryItem'` | `(self, retry_id: 'int', *, status: 'str', media_path: 'str | None' = None, last_http_status: 'int | None' = None, last_error: 'str | None' = None, next_attempt_time: 'str | None' = None) -> 'MediaRetryItem'` | yes | direct |
| `redact_claim_payload` | `(self, claim_id: 'int', *, mode: 'str' = 'redact', redact_claim: 'bool' = True, redact_citations: 'bool' = True, reason: 'str | None' = None, actor: 'str' = 'system') -> 'dict[str, object]'` | `(self, claim_id: 'int', *, mode: 'str' = 'redact', redact_claim: 'bool' = True, redact_citations: 'bool' = True, reason: 'str | None' = None, actor: 'str' = 'system') -> 'dict[str, object]'` | yes | direct |
| `remove_claim_link` | `(self, source_id: 'int', target_id: 'int', link_type: 'str | None' = None) -> 'int'` | `(self, source_id: 'int', target_id: 'int', link_type: 'str | None' = None) -> 'int'` | yes | direct |
| `resolve_claim_id` | `(self, identifier: 'str | int') -> 'int'` | `(self, identifier: 'str | int') -> 'int'` | yes | direct |
| `set_confidence` | `(self, claim_id: 'int', confidence: 'float', details: 'str | None' = None) -> 'None'` | `(self, claim_id: 'int', confidence: 'float', details: 'str | None' = None) -> 'None'` | yes | direct |
| `set_evidence_item_sensitivity` | `(self, evidence_item_row_id: 'int', sensitivity: 'str | None') -> 'EvidenceItem'` | `(self, evidence_item_row_id: 'int', sensitivity: 'str | None') -> 'EvidenceItem'` | yes | direct |
| `set_normalized_text` | `(self, claim_id: 'int', normalized_text: 'str') -> 'None'` | `(self, claim_id: 'int', normalized_text: 'str') -> 'None'` | yes | direct |
| `set_normalized_texts_batch` | `(self, updates: 'dict[int, str]') -> 'None'` | `(self, updates: 'dict[int, str]') -> 'None'` | yes | direct |
| `set_pinned` | `(self, claim_id: 'int', pinned: 'bool', reason: 'str') -> 'None'` | `(self, claim_id: 'int', pinned: 'bool', reason: 'str') -> 'None'` | yes | direct |
| `set_source_item_sensitivity` | `(self, source_item_row_id: 'int', sensitivity: 'str | None') -> 'SourceItem'` | `(self, source_item_row_id: 'int', sensitivity: 'str | None') -> 'SourceItem'` | yes | direct |
| `set_supersedes` | `(self, claim_id: 'int', supersedes_claim_id: 'int') -> 'None'` | `(self, claim_id: 'int', supersedes_claim_id: 'int') -> 'None'` | yes | direct |
| `traverse_relationships` | `(self, start_claim_id: 'int', *, link_types: 'list[str] | None' = None, max_depth: 'int' = 3, direction: 'str' = 'both') -> 'list[dict]'` | `(self, start_claim_id: 'int', *, link_types: 'list[str] | None' = None, max_depth: 'int' = 3, direction: 'str' = 'both') -> 'list[dict]'` | yes | inherited:_ReadMixin |
| `update_action_proposal_fields` | `(self, proposal_id: 'int', *, title: 'str | None' = None, description: 'str | None' = None, suggested_due_at: 'str | None' = None, confidence: 'float | None' = None, payload_json: 'dict[str, Any] | str | None' = None) -> 'ActionProposal'` | `(self, proposal_id: 'int', *, title: 'str | None' = None, description: 'str | None' = None, suggested_due_at: 'str | None' = None, confidence: 'float | None' = None, payload_json: 'dict[str, object] | str | None' = None) -> 'ActionProposal'` | no | direct |
| `update_action_proposal_status` | `(self, proposal_id: 'int', *, status: 'str', external_ref: 'str | None' = None, exported_at: 'str | None' = None, payload_json: 'dict[str, Any] | str | None' = None) -> 'ActionProposal'` | `(self, proposal_id: 'int', *, status: 'str', external_ref: 'str | None' = None, exported_at: 'str | None' = None, payload_json: 'dict[str, object] | str | None' = None) -> 'ActionProposal'` | no | direct |
| `update_claim_structure` | `(self, claim_id: 'int', *, claim_type: 'str | None' = None, subject: 'str | None' = None, predicate: 'str | None' = None, object_value: 'str | None' = None) -> 'None'` | `(self, claim_id: 'int', *, claim_type: 'str | None' = None, subject: 'str | None' = None, predicate: 'str | None' = None, object_value: 'str | None' = None) -> 'None'` | yes | direct |
| `upsert_embeddings` | `(self, claims: 'list[Claim]', provider: 'EmbeddingProvider') -> 'int'` | `(self, claims: 'list[Claim]', provider: 'EmbeddingProvider') -> 'int'` | yes | direct |
| `upsert_external_source` | `(self, *, source_type: 'str', display_name: 'str', config_json: 'dict[str, Any] | str | None' = None) -> 'ExternalSource'` | `(self, *, source_type: 'str', display_name: 'str', config_json: 'dict[str, object] | str | None' = None) -> 'ExternalSource'` | no | direct |
| `upsert_source_item` | `(self, *, source_id: 'int', source_item_id: 'str', item_type: 'str', chat_id: 'str | None' = None, sender_id: 'str | None' = None, sender_name: 'str | None' = None, occurred_at: 'str | None' = None, text: 'str | None' = None, payload_json: 'dict[str, Any] | str | None' = None, content_hash: 'str | None' = None, sensitivity: 'str | None' = None) -> 'SourceItem'` | `(self, *, source_id: 'int', source_item_id: 'str', item_type: 'str', chat_id: 'str | None' = None, sender_id: 'str | None' = None, sender_name: 'str | None' = None, occurred_at: 'str | None' = None, text: 'str | None' = None, payload_json: 'dict[str, object] | str | None' = None, content_hash: 'str | None' = None, sensitivity: 'str | None' = None) -> 'SourceItem'` | no | direct |
| `vector_scores` | `(self, query_text: 'str', claims: 'list[Claim]', provider: 'EmbeddingProvider') -> 'dict[int, float]'` | `(self, query_text: 'str', claims: 'list[Claim]', provider: 'EmbeddingProvider') -> 'dict[int, float]'` | yes | direct |

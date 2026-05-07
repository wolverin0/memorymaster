# Atlas Inbox V1 Technical Spec

## Context

This spec translates the Atlas Inbox PRD into a MemoryMaster implementation plan.
The product wedge is:

> WhatsApp-first people and small businesses need searchable, source-backed memory and approved task extraction from chats, voice notes, screenshots, receipts, and documents.

MemoryMaster remains the trustworthy memory kernel. Atlas Inbox is the user-facing inbox/dashboard. Super Productivity is the task execution layer.

## Non-Negotiables

- Local-first by default. Raw WhatsApp/media data stays on device unless the user explicitly enables cloud processing.
- Every extracted claim and action proposal must link back to source evidence.
- External action execution is approval-gated. No automatic export or execution without review.
- MemoryMaster must not become a Zapier clone. It stores evidence, claims, conflicts, sensitivity, and proposals; external gateways execute approved actions.
- The MVP is WhatsApp -> MemoryMaster -> AI Inbox -> Super Productivity. Gmail, calendars, CRM, team SaaS, and calorie/food modules are later.

## Current Repo Fit

MemoryMaster already has these useful primitives:

- `claims` plus `citations` for source-backed facts.
- An append-only `events` table with event hashes and payload validation.
- `Claim`, `Citation`, and `Event` models in `memorymaster/models.py`.
- `MemoryService.ingest()` and `SQLiteStore.create_claim()` for claim creation.
- `auto_extractor.py` for LLM claim extraction patterns.
- `operator_queue.py` / `operator.py` for review-style workflows.
- `dashboard.py` for an existing UI surface.

The V1 design should extend these primitives instead of creating a disconnected product database.

## Conversation-Derived Decisions

These decisions came from the full ChatGPT shared conversation plus the PRD:

- Do not frame Atlas Inbox as a better ChatGPT or better Claude. Frame it as a private context/memory/action layer that can use OpenAI, Claude, Gemini, or local models as replaceable brains.
- Start from a narrow wedge: WhatsApp/media -> memory -> reviewable actions. Keep the larger life operating system vision, but do not build a generic life OS first.
- Prefer hybrid local-first architecture: desktop/local service and local encrypted storage first, optional cloud later.
- Treat `wacli` and `wacrawl` as different connector modes:
  - `wacli`: better for live sync, WhatsApp Web-style capture, JSON/SQLite output, and future automation.
  - `wacrawl`: better for read-only local archive, macOS WhatsApp Desktop data, and encrypted Git backup/archive workflows.
- `wacli` can expose media metadata and can download media, but transcription/OCR/vision are Atlas/MemoryMaster processors, not built-in `wacli` capabilities.
- Use Super Productivity as the execution/task layer, not as the long-term memory database.
- Use One/Pica/Zapier/Make/n8n/MCP-style gateways for approved external actions later; do not make MemoryMaster an integration platform.
- Keep the product general for personal users, SMBs, teams, and WhatsApp-first businesses. ISP/WISP workflows can be a business template later, not the default product identity.

## V1 Goal

Prove that WhatsApp can become useful memory and tasks.

The first working vertical slice should import a small WhatsApp export, store messages/media as external source events and evidence, extract candidate claims/actions, review them, and export approved tasks to Super Productivity with source notes.

## Implementation Status

The first backend vertical slice is implemented in MemoryMaster:

- Source/evidence/action proposal schema for SQLite and Postgres.
- `wacli`-style WhatsApp JSON/JSONL importer.
- Mock transcription and OCR provider interfaces.
- Deterministic Atlas claim extraction from evidence.
- Deterministic action proposal extraction from evidence.
- CLI review workflow for listing and resolving action proposals.
- Approved-action export to an Atlas/Super Productivity bridge JSON file.

Implemented commands:

- `python -m memorymaster --db memorymaster.db import-whatsapp --input path/to/export.json`
- `python -m memorymaster --db memorymaster.db extract-atlas-claims`
- `python -m memorymaster --db memorymaster.db propose-actions`
- `python -m memorymaster --db memorymaster.db action-proposals --status candidate`
- `python -m memorymaster --db memorymaster.db resolve-action-proposal --proposal-id 1 --status approved`
- `python -m memorymaster --db memorymaster.db export-actions --output artifacts/atlas/super-productivity.json`

## Domain Model

The PRD model maps to MemoryMaster as follows:

| PRD concept | MemoryMaster V1 representation |
| --- | --- |
| Event | Append-only external event/source item record |
| Evidence | Evidence/source item linked to event and optional claim/action |
| Claim | Existing `claims` row with citations and lifecycle status |
| Entity | Existing/future entity registry plus extracted chat/contact/person metadata |
| Relationship | Existing `claim_links` plus later entity graph edges |
| Action Proposal | New approval-gated proposal record linked to source event/evidence/claim |

## Proposed Storage Additions

Keep the existing `events` table for audit/lifecycle events. Add product-facing source tables so WhatsApp messages and media can be queried without overloading claim lifecycle events.

### `external_sources`

Stores connected/imported systems.

- `id`
- `source_type`: `whatsapp`, later `gmail`, `calendar`, `files`
- `display_name`
- `config_json`: sanitized non-secret config only
- `created_at`
- `updated_at`

### `source_items`

Stores imported messages/documents/media envelopes.

- `id`
- `source_id`
- `source_item_id`: stable external id from `wacli`/`wacrawl`
- `item_type`: `message`, `audio`, `image`, `document`
- `chat_id`
- `sender_id`
- `sender_name`
- `occurred_at`
- `text`
- `payload_json`: normalized metadata
- `content_hash`
- `created_at`
- unique `(source_id, source_item_id)`

### `evidence_items`

Stores source-grounded derived text and pointers.

- `id`
- `source_item_id`
- `evidence_type`: `message_text`, `transcript`, `ocr`, `receipt_parse`, `document_text`
- `text`
- `media_path`
- `provider`
- `confidence`
- `payload_json`
- `created_at`

### `action_proposals`

Stores human-reviewable actions before export.

- `id`
- `proposal_type`: `task`, `reminder`, `follow_up`, `draft_reply`, `support_ticket`
- `title`
- `description`
- `source_item_id`
- `evidence_item_id`
- `claim_id`
- `suggested_due_at`
- `destination`: `super-productivity`, `manual`, later CRM/calendar
- `status`: `candidate`, `approved`, `rejected`, `exported`, `failed`
- `confidence`
- `payload_json`
- `exported_at`
- `external_ref`
- `created_at`
- `updated_at`

### Event Types

Add event types only where audit history is needed:

- `source_import`
- `media_process`
- `action_proposal`
- `action_export`

Use those for summary/audit rows in the append-only `events` table while the structured source/proposal tables hold product state.

## Milestone 1: External Event/Evidence Layer

Deliverables:

- Add storage migrations for `external_sources`, `source_items`, `evidence_items`, and `action_proposals`.
- Add dataclasses/models for source items, evidence items, and action proposals.
- Add store/service methods:
  - `upsert_external_source`
  - `upsert_source_item`
  - `add_evidence_item`
  - `create_action_proposal`
  - `update_action_proposal_status`
  - list/query helpers for dashboard use
- Add tests for idempotent import, evidence linkage, proposal lifecycle, and audit events.

Acceptance:

- Re-importing the same source item does not duplicate rows.
- Evidence can be traced to the source item.
- Candidate action proposals can be approved/rejected/exported without mutating source evidence.

## Milestone 2: WhatsApp Importer

Start with one supported export format, then add adapters.

Recommended first target: `wacli` JSON export, because it is easier to fixture and test than live crawling.

Connector expectations:

- `wacli` history is best-effort. The importer must not assume complete WhatsApp history.
- `wacli` should be treated as an unofficial third-party WhatsApp Web-protocol source, with a clear user-facing warning.
- `wacrawl` should be a separate adapter, not mixed into the first importer. Its value is local read-only archive and encrypted Git backup support.
- Importers should preserve enough original identifiers to support re-import, dedupe, and source traceability.

Deliverables:

- Add `memorymaster/connectors/whatsapp.py`.
- Normalize chats, senders, timestamps, text, message IDs, and media metadata.
- CLI command:
  - `python -m memorymaster --db memorymaster.db import-whatsapp --input path/to/export.json`
- Fixtures with:
  - plain text messages
  - group chat messages
  - voice note metadata
  - image/receipt metadata
  - duplicate messages

Acceptance:

- Imports text messages into `source_items`.
- Creates message-text `evidence_items`.
- Preserves chat/sender/timestamp metadata.
- Deduplicates by source id and/or content hash.

## Milestone 3: Media Processing Interfaces

Deliverables:

- Add provider interfaces:
  - `TranscriptionProvider.transcribe(path) -> EvidenceResult`
  - `OcrProvider.extract(path) -> EvidenceResult`
- Add mock/local providers first so tests do not require paid APIs.
- Store transcripts and OCR output as `evidence_items`.
- Add processing status metadata to source item payloads or a separate processing table if needed.
- Support the media path shape produced by the connector, but keep processors connector-agnostic.

Acceptance:

- Voice note fixture produces transcript evidence.
- Image fixture produces OCR evidence.
- Provider failures are recorded without losing the source item.
- Media metadata can exist before the actual file is downloaded.

## Milestone 4: Claim Extraction

Deliverables:

- Add WhatsApp-oriented extraction templates for:
  - commitments/promises
  - complaints/problems
  - payment proof mentions
  - quote/price requests
  - deadlines/dates
  - follow-up requests
- Reuse existing claim ingestion so every claim has citations.
- Citation source should identify the source item/evidence item, for example:
  - `whatsapp://<source>/<chat>/<message>`
  - locator: timestamp/message id
  - excerpt: message/transcript/OCR snippet

Acceptance:

- Extracted claims are `candidate` claims.
- Each claim has at least one citation.
- Claim scope is configurable, defaulting to the import workspace/project.

## Milestone 5: Action Proposals

Deliverables:

- Extract candidate actions from evidence/claims:
  - task
  - reminder
  - follow-up
  - verify receipt
  - draft reply
- Store proposals in `action_proposals`.
- Link proposals to source item, evidence item, and claim where available.
- Add idempotency so the same message does not create duplicate proposals.

Acceptance:

- Proposed actions are reviewable before export.
- Rejecting a proposal does not delete source/evidence/claim rows.
- Approval creates an audit event.

## Milestone 6: AI Inbox Review

Use the current dashboard as the first review surface before building a full Atlas Inbox app.

Deliverables:

- Add list/filter endpoints or service methods for action proposals.
- Dashboard view:
  - candidate proposals
  - source excerpt
  - confidence
  - approve/reject/edit
  - mark sensitive
  - export status

Acceptance:

- User can approve, reject, and edit candidate actions.
- Source evidence is visible next to the proposal.
- Sensitive items can be withheld from cloud processing/export.

## Milestone 7: Super Productivity Export

Deliverables:

- Export approved proposals to Super Productivity using the simplest reliable local integration.
- Include source notes:
  - chat/contact
  - timestamp
  - excerpt/transcript/OCR
  - MemoryMaster claim/action ids
  - confidence
- Store `external_ref` and `exported_at`.
- Prevent duplicate exports.

Acceptance:

- Approved tasks appear in Super Productivity or its import file format.
- Re-exporting does not create duplicates.
- Failed exports keep the proposal in `failed` with error detail.

## V2 Integration Gateway

V1 should prove memory and reviewable proposals before depending on broad integration gateways. V2 should add a formal action gateway layer.

Gateway candidates:

- One CLI / One MCP
- Pica
- Zapier
- Make
- n8n
- native APIs for high-value direct integrations

Gateway workflow:

```text
approved action proposal
  -> choose destination app
  -> read action schema/knowledge
  -> execute through gateway/native API
  -> log result back into MemoryMaster
```

Examples:

- Detected appointment request -> propose calendar event -> create event after approval -> draft confirmation.
- Detected payment receipt -> extract amount -> create CRM/payment note -> create review task.
- Detected support issue -> create ticket/task -> attach source evidence.

Native connectors remain reserved for high-volume/private memory sources such as WhatsApp, files, receipts, email, calendar, and documents. External gateways are for action execution after approval.

## Suggested Implementation Order

1. Add storage models and tests for sources/evidence/proposals.
2. Implement `wacli` JSON importer with fixtures.
3. Wire text-message evidence into existing claim extraction.
4. Add deterministic/simple action extraction before LLM extraction.
5. Add dashboard review and status transitions.
6. Add Super Productivity export.
7. Add audio/OCR providers after the text-message path is stable.

## First PR Scope

Keep the first PR narrow:

- schema and storage methods for `external_sources`, `source_items`, `evidence_items`, `action_proposals`
- model dataclasses
- tests for idempotency and lifecycle
- no live WhatsApp dependency
- no cloud model dependency
- no dashboard changes

This creates the foundation for the next vertical slice without taking on ingestion, AI, and UI in one change.

## Risks

- WhatsApp export formats may vary. Use adapter boundaries and fixtures.
- Media files can leak sensitive data. Default to local paths and explicit redaction/sensitivity controls.
- Claims and action proposals are different things. Do not force tasks into the claim lifecycle.
- Dashboard scope can balloon. Start with review mechanics only.
- Super Productivity integration details may change. Keep an export abstraction and record idempotency metadata.

## Open Decisions Before First PR

- Exact first WhatsApp export format: `wacli` JSON vs SQLite.
- Whether source/proposal tables also need Postgres parity immediately.
- Whether proposal status should reuse operator queue concepts or stay as a separate domain table.
- Where local media files are stored and how paths are normalized on Windows/WSL.
- Minimum dashboard endpoint shape for review.

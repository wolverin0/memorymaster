# Atlas Inbox API/CLI Contract — v1.5.0

**Audience:** LifeAgent (and any other Atlas frontend) consuming MemoryMaster's Atlas Inbox backend.

**Stable since:** 2026-05-05.

**Source of truth:** `memorymaster/atlas_contract.py`. The values in this doc must match that module — the test suite (`tests/test_atlas_contract.py`) enforces the match.

---

## 1. Versioning policy

The contract uses semver. Consumers MUST refuse to start if the **major** version returned by `GET /api/atlas/version` (or `python -m memorymaster --json atlas-version`) does not match the major they were compiled against.

| Bump kind | When | Consumer action |
|---|---|---|
| **MAJOR** | Removed/renamed CLI flag, removed envelope field, type change, removed HTTP endpoint, method change, semantics change | Refuse to start; reimplement |
| **MINOR** | Added new CLI subcommand, added new HTTP endpoint, added new envelope field (additive only) | Continue to work; opt-in to new field |
| **PATCH** | Behavioural fix that does not change the contract surface | No change required |

`atlas_contract.BREAKING_CHANGES_SINCE` lists every MAJOR bump in chronological order. Empty in 1.0.0 — the contract was born here.

---

## 2. Discover the contract at runtime

Every consumer should hit one of these on startup and log/refuse on major mismatch:

- **CLI:** `python -m memorymaster --json atlas-version`
- **HTTP:** `GET /api/atlas/version`

Both return the full contract spec (subcommands, endpoints, version, breaking-change history). The dashboard response wraps it as `{"ok": true, ...spec}`; the CLI wraps it in the standard envelope (see §3).

---

## 3. Standard CLI JSON envelope

Every Atlas CLI subcommand invoked with `--json` returns:

```json
{
  "ok": true,
  "data": <subcommand-specific>,
  "meta": {
    "query_ms": <float>,
    "total": <int|omitted>,
    "atlas_contract_version": "1.0.0",
    "atlas_subcommand": "<subcommand-name>"
  }
}
```

Stable field guarantees:

- `ok` is always present and is `true` on success. Errors come on stderr / non-zero exit.
- `data` is always present.
- `meta.atlas_contract_version` and `meta.atlas_subcommand` are guaranteed on every Atlas subcommand and let the consumer cross-check the producer.
- `meta.query_ms` is always present (rounded to 2 decimals).
- `meta.total` is present where the subcommand has a natural "row count" — see the `meta_total` column in §4.

Non-Atlas subcommands (the rest of MemoryMaster) emit the same envelope **without** the `atlas_contract_version`/`atlas_subcommand` fields. Consumers should not assume those fields outside Atlas.

---

## 4. CLI subcommands

| Subcommand | Inputs | `data` shape | `meta.total` |
|---|---|---|---|
| `init-db` | (none) | `{db, stealth}` | (omitted) |
| `import-whatsapp` | `--input <path>` (req), `--display-name` (def `WhatsApp`), `--chat-id` | `{source_id, source_items_seen, source_items_imported, source_items_updated, evidence_items_added, duplicates_seen}` | `source_items_seen` |
| `extract-atlas-claims` | `--scope` (def: derives `project:<cwd-basename>`), `--limit` (def 200) | `{scanned, matched, ingested, claims:[Claim]}` | `ingested` |
| `propose-actions` | `--destination` (def `super-productivity`), `--limit` (def 200) | `{scanned, matched, created, existing, proposals:[ActionProposal]}` | `created` |
| `action-proposals` | `--status` (one of `candidate/approved/rejected/exported/failed`), `--destination`, `--limit` (def 100) | `[ActionProposal]` | `len(data)` |
| `resolve-action-proposal` | `--proposal-id <int>` (req), `--status` (one of statuses, req), `--external-ref` | `ActionProposal` | `1` |
| `edit-action-proposal` | `--proposal-id <int>` (req), `--title` (non-blank if provided), `--description`, `--suggested-due-at` (ISO-8601), `--confidence` (0.0-1.0). At least one field required. | `ActionProposal` | `1` |
| `label-source-item` | `--source-item-id <int>` (req), `--sensitivity {none,low,medium,high,redacted,clear}` (req) | `SourceItem` | `1` |
| `label-evidence-item` | `--evidence-item-id <int>` (req), `--sensitivity {none,low,medium,high,redacted,clear}` (req) | `EvidenceItem` | `1` |
| `enqueue-media-retry` | `--source-item-id <int>` (req), `--media-key <str>` (req), `--chat-id`, `--media-type`, `--media-path`, `--media-url`, `--next-attempt-time` (ISO-8601) | `MediaRetryItem` | `1` |
| `process-media-retry-queue` | `--limit <int>` (def 25) | `{attempted, expired, recovered, failed, pending_remaining, rows:[MediaRetryItem]}` | `attempted` |
| `record-media-retry-outcome` | `--retry-id <int>` (req), `--status {pending,retrying,expired,done,failed}` (req), `--media-path` (required for `done`), `--last-http-status`, `--last-error`, `--next-attempt-time` | `MediaRetryItem` | `1` |
| `list-media-retries` | `--status`, `--source-item-id`, `--limit` (def 100) | `list[MediaRetryItem]` | `len(data)` |
| `transcribe-source-item` | `--source-item-id <int>` (req), `--provider {mock,openai}` (req) | `{source_item_id, created, evidence, error, provider}` | `1 if evidence else 0` |
| `ocr-source-item` | `--source-item-id <int>` (req), `--provider {mock,tesseract}` (req) | same shape | same |
| `export-actions` | `--output <path>` (req), `--destination` (def `super-productivity`), `--limit` (def 100), `--dry-run` | `{destination, output_path, exported, proposal_ids:[int]}` | `exported` |
| `atlas-version` | (none) | `{atlas_contract_version, atlas_contract_name, subcommands, endpoints, breaking_changes_since}` | `1` |

### Sensitivity labels on `source_items` and `evidence_items`

Both tables expose a `sensitivity` field. Allowed values:

| Value | Meaning |
|---|---|
| `null` | Unlabeled — never inspected |
| `"none"` | Inspected, verified non-sensitive |
| `"low"` | Mildly sensitive |
| `"medium"` | Moderately sensitive |
| `"high"` | High sensitivity |
| `"redacted"` | Content has been redacted at the application layer |

Set via `label-source-item` / `label-evidence-item` CLIs, or via the service methods `set_source_item_sensitivity` / `set_evidence_item_sensitivity`. **Re-importing a labeled `source_item` via `import-whatsapp` PRESERVES the label** unless the importer explicitly passes a new sensitivity — operator decisions are sticky.

LifeAgent should treat these as authoritative backend labels and use them to filter/display review surfaces.

### Real provider adapters (v1.5.0)

`memorymaster/media_providers.py` ships two real adapters behind the existing `TranscriptionProvider` / `OcrProvider` `Protocol`s. Production has no synthetic fallback: callers select a ready real provider explicitly.

| Adapter | Class | Optional dependency | Env vars |
|---|---|---|---|
| OpenAI Whisper transcription | `OpenAIWhisperTranscriptionProvider` | None (stdlib only — urllib + manual multipart) | `OPENAI_API_KEY` (required at call time), `OPENAI_BASE_URL` (default `https://api.openai.com/v1`) |
| Tesseract OCR | `TesseractOcrProvider` | `pytesseract` Python package + system `tesseract` binary | None |

**Readiness:** importing the module remains safe. The CLI factory checks credentials and dependencies before processing; direct provider calls retain actionable runtime checks. Missing configuration never produces placeholder evidence.

**Factory:**

```python
from memorymaster.media_providers import get_transcription_provider, get_ocr_provider
provider = get_transcription_provider("openai")    # or "mock"
provider = get_ocr_provider("tesseract")            # or "mock"
```

Mocks are test/development-only and require both `MEMORYMASTER_MEDIA_MODE=test` (or `development`) and `MEMORYMASTER_ALLOW_SYNTHETIC_MEDIA=1`. Synthetic evidence is excluded from claim extraction, LLM prompts, action proposals, citations, and action export.

**CLI:**

```bash
# Transcribe an audio source_item via Whisper
python -m memorymaster --db /data/atlas-inbox.db --json transcribe-source-item \
  --source-item-id 42 --provider openai

# OCR an image source_item via tesseract
python -m memorymaster --db /data/atlas-inbox.db --json ocr-source-item \
  --source-item-id 99 --provider tesseract
```

Both CLIs run the existing `process_transcription` / `process_ocr` pipeline. Failures are recorded as `media_process` events with `details="media_process_failed"` and return a non-zero exit code. Idempotency is provider-specific, so an old mock row cannot block later real enrichment.

### Media retry queue (v1.4.0)

**Architecture:** MemoryMaster owns durable queue STATE; LifeAgent/wacli owns the actual WhatsApp media download. There is **no HTTP fetcher in MemoryMaster**.

**Workflow:**

1. wacli reports a missing/failing media → LifeAgent calls `enqueue-media-retry` (idempotent on `(source_item_id, media_key)`).
2. Periodic tick: LifeAgent calls `process-media-retry-queue --limit N`. Pending rows whose `next_attempt_time` has passed are atomically promoted to `retrying` and `attempt_count++`. Returns the claimed rows so LifeAgent knows what to fetch.
3. LifeAgent fetches each via wacli, then calls `record-media-retry-outcome --retry-id N --status X` per row:
   - `done` → success; **`--media-path` required**.
   - `expired` → terminal (HTTP 403/410 — WhatsApp media is gone).
   - `failed` → gave up but not WhatsApp-terminal.
   - `pending` → transient failure, retry later (set `--next-attempt-time`).

**Status semantics:**

| Status | Meaning |
|---|---|
| `pending` | Enqueued, awaiting `next_attempt_time` |
| `retrying` | Claimed by `process-media-retry-queue`; LifeAgent is fetching |
| `done` | LifeAgent reported success; `media_path` populated |
| `expired` | Terminal — WhatsApp returned 403/410 |
| `failed` | LifeAgent gave up (max attempts, etc.) |

**Critical guarantee:** Text/source imports continue working even when media retries fail. The queue tracks ONLY media-fetch state; it does not block claim extraction or proposal generation from text evidence.

**Audit:** every state transition records a `media_process` event with `from_status` / `to_status` and a payload including `retry_id`. Inspectable via `list-events --event-type media_process`.

### `ActionProposal` row shape

Every CLI/endpoint that returns an action proposal returns this dict shape:

```jsonc
{
  "id": 42,
  "proposal_type": "task",                 // task|reminder|follow_up|draft_reply|support_ticket
  "title": "Send installation quote",
  "description": "Source-backed action proposal extracted from evidence...",
  "source_item_id": 7,                     // FK into source_items, may be null after delete
  "evidence_item_id": 11,                  // FK into evidence_items, may be null
  "claim_id": 36320,                       // FK into claims, may be null
  "suggested_due_at": "2026-05-06T12:00:00-03:00",  // ISO-8601 or null
  "destination": "super-productivity",
  "status": "candidate",                   // candidate|approved|rejected|exported|failed
  "confidence": 0.81,
  "payload_json": "{\"extractor\":\"atlas-rule-v1\",...}",
  "exported_at": null,                     // ISO-8601 once exported
  "external_ref": null,                    // e.g. "file:./out.json#proposal-42" after export
  "idempotency_key": "evidence:11:task:abc123def456...",
  "created_at": "2026-05-05T15:00:00+00:00",
  "updated_at": "2026-05-05T15:00:00+00:00"
}
```

### `Claim` row shape

Atlas-extracted claims follow the standard MemoryMaster `Claim` shape (see `memorymaster/models.py:Claim`). The Atlas-specific guarantees:

- Every Atlas claim has at least one `Citation` whose `source` is `whatsapp://source/<source-id>/item/<external-id>` and `locator` is `evidence:<evidence-id>`.
- `status` starts at `"candidate"`.
- `scope` defaults to `project:<cwd-basename>` derived from the working directory; pass `--scope` to override.

### Super-Productivity bridge JSON (`export-actions --output`)

The exported file is consumed by Super Productivity (or any equivalent task system). Shape:

```jsonc
{
  "format": "atlas-super-productivity-bridge-v1",
  "destination": "super-productivity",
  "tasks": [
    {
      "title": "Send installation quote",
      "notes": "Source-backed action proposal...\n\nAtlas source_item_id: 7\nAtlas evidence_item_id: 11",
      "due": "2026-05-06T12:00:00-03:00",   // ISO-8601 or null
      "atlas_proposal_id": 42,
      "atlas_confidence": 0.81,
      "atlas_payload": {"extractor": "atlas-rule-v1", "...": "..."}
    }
  ]
}
```

The `format` string is the contract handshake — bump it to `atlas-super-productivity-bridge-v2` on any breaking change to this file.

---

## 5. Dashboard HTTP endpoints

Default base URL when `python -m memorymaster --db memorymaster.db dashboard` is running: `http://localhost:8765` (configurable).

### `GET /api/action-proposals`

List Atlas action proposals.

| Query param | Type | Required | Default | Notes |
|---|---|---|---|---|
| `status` | str | no | (any) | One of `candidate/approved/rejected/exported/failed` |
| `destination` | str | no | (any) | e.g. `super-productivity` |
| `limit` | int | no | 100 | clamped to `[1, 500]` |

Response:

```json
{
  "ok": true,
  "rows": <int>,
  "proposals": [<ActionProposal>, ...]
}
```

### `POST /api/action-proposals/status`

Update an Atlas action proposal status (approve/reject/export/fail).

Request body:

```json
{
  "proposal_id": <int>,                  // required, > 0
  "status": "approved",                  // required, one of candidate/approved/rejected/exported/failed
  "external_ref": "sp-task-1"            // optional
}
```

Response:

```json
{ "ok": true, "proposal": <ActionProposal> }
```

Errors return `{"ok": false, "error": "<message>"}` with HTTP 400 on validation errors and 404 on not-found.

### `GET /api/atlas/version`

Returns the full Atlas contract spec for runtime version handshake.

Response:

```json
{
  "ok": true,
  "atlas_contract_version": "1.0.0",
  "atlas_contract_name": "atlas-inbox-v1",
  "subcommands": [...],
  "endpoints": [...],
  "breaking_changes_since": []
}
```

---

## 5b. Canonical fixture for consumer tests

`tests/fixtures/atlas/whatsapp_wacli_basic.json` is the canonical wacli-style
WhatsApp fixture. It contains:

- 3 plain text messages (Spanish, including action-trigger and complaint
  patterns the deterministic extractors recognize)
- 1 audio message with media metadata
- 1 image message with media metadata + caption
- 1 duplicate row that must be deduplicated by the importer

LifeAgent (and any other consumer) is welcome to copy this fixture into its
own test suite for end-to-end pipeline assertions. `tests/test_atlas_contract.py`
exercises the full `import → extract → propose → list → resolve → export`
chain against this fixture and pins the envelope shapes — break the chain in
MemoryMaster and the test fails.

## 6. Out-of-scope

What this contract does **not** cover (handled elsewhere):

- **Frontend dashboard / review UI** — owned by LifeAgent. MemoryMaster only ships backend.
- **MemoryMaster's general claim/citation/event API** — see `memorymaster/service.py:MemoryService`. Atlas extracted claims live in the regular `claims` table and are reachable via the standard MemoryMaster query/wiki tooling.
- **Real transcription/OCR provider implementations** — only `Mock*` ship in v1.0.0. Real adapters will plug into the existing `TranscriptionProvider`/`OcrProvider` `Protocol`s in `memorymaster/media_processing.py`.
- **Connector lifecycle (auth, sync, push)** — v1.0.0 ships only the wacli JSON/JSONL importer (`import-whatsapp`). Future connectors are independent additions.

---

## 7. How to evolve the contract safely

When you need to ship a change:

1. Read `memorymaster/atlas_contract.py` and decide MAJOR/MINOR/PATCH per §1.
2. Update the relevant entry in `ATLAS_SUBCOMMANDS` or `ATLAS_ENDPOINTS`.
3. Bump `ATLAS_CONTRACT_VERSION`.
4. If MAJOR: append to `BREAKING_CHANGES_SINCE` with `{version, summary, date}`.
5. Update this doc in the same PR.
6. Update `tests/test_atlas_contract.py` with new field assertions (additive) or version bump.
7. Run `python -m pytest tests/test_atlas_contract.py -q` — must be green before merge.

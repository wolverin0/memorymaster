"""Atlas Inbox API/CLI contract.

This module is the **single source of truth** for the stable backend contract
that LifeAgent (and any other Atlas frontend) depends on. The contract covers:

- CLI subcommand inputs and JSON output envelope shape
- Dashboard HTTP endpoints (path, method, request payload, response payload)
- The full list of fields each surface is guaranteed to return
- The semver-style version string surfaced via ``atlas-version`` and
  ``GET /api/atlas/version``

## Versioning policy

We use semver. Any consumer MUST refuse to start if the major version emitted
by ``GET /api/atlas/version`` (or ``atlas-version``) does not match what the
consumer was compiled against.

- **MAJOR** bump: removed or renamed a CLI flag, removed an envelope field,
  changed an envelope field's type, removed an HTTP endpoint, changed an
  HTTP method, changed an existing field's semantics.
- **MINOR** bump: added a new CLI subcommand, added a new HTTP endpoint,
  added a new field to an envelope (additive only).
- **PATCH** bump: behavioural fix that does not change the contract surface
  (e.g., performance improvement, default-value change that is still in the
  documented allowed range).

Every Atlas CLI handler emits ``meta.atlas_contract_version`` and
``meta.atlas_subcommand`` so consumers can sanity-check the producer they're
talking to.

## Adding a new Atlas subcommand or endpoint

1. Add an entry to ``ATLAS_SUBCOMMANDS`` or ``ATLAS_ENDPOINTS`` below.
2. Bump ``ATLAS_CONTRACT_VERSION`` to the next minor version.
3. Update ``docs/atlas-api-contract-v1.md`` with the new shape.
4. Add a contract test in ``tests/test_atlas_contract.py`` that pins the
   envelope keys.
5. Never remove an entry; mark it ``"deprecated_since": "X.Y.Z"`` instead and
   bump the major when the entry is finally removed.
"""
from __future__ import annotations

from typing import Any

ATLAS_CONTRACT_VERSION = "1.5.0"
"""Semver string for the Atlas API/CLI contract.

LifeAgent and any other consumer MUST refuse to start if the major component
of this string differs from what they were compiled against.
"""

ATLAS_CONTRACT_NAME = "atlas-inbox-v1"
"""Stable namespace name. Never changes within v1.x.x."""


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------

ATLAS_SUBCOMMANDS: list[dict[str, Any]] = [
    {
        "name": "init-db",
        "description": "Initialize the MemoryMaster DB at --db path (creates ALL tables including Atlas).",
        "inputs": {},
        "data_keys": ["db", "stealth"],
        "meta_total": "(omitted)",
        "note": "init-db is general MemoryMaster, not Atlas-specific, but listed here so consumers like LifeAgent can call it as part of their bootstrap.",
    },
    {
        "name": "import-whatsapp",
        "description": "Import WhatsApp messages from a wacli JSON/JSONL export.",
        "inputs": {
            "--input": {"type": "path", "required": True},
            "--display-name": {"type": "str", "required": False, "default": "WhatsApp"},
            "--chat-id": {"type": "str", "required": False, "default": None},
        },
        "data_keys": [
            "source_id",
            "source_items_seen",
            "source_items_imported",
            "source_items_updated",
            "evidence_items_added",
            "duplicates_seen",
        ],
        "meta_total": "source_items_seen",
    },
    {
        "name": "extract-atlas-claims",
        "description": "Extract candidate claims from Atlas evidence.",
        "inputs": {
            "--scope": {
                "type": "str",
                "required": False,
                "default": None,
                "note": "When omitted, derives project:<cwd-basename> via scope_from_cwd.",
            },
            "--limit": {"type": "int", "required": False, "default": 200},
        },
        "data_keys": ["scanned", "matched", "ingested", "claims"],
        "meta_total": "ingested",
    },
    {
        "name": "propose-actions",
        "description": "Create reviewable action proposals from Atlas evidence.",
        "inputs": {
            "--destination": {"type": "str", "required": False, "default": "super-productivity"},
            "--limit": {"type": "int", "required": False, "default": 200},
        },
        "data_keys": ["scanned", "matched", "created", "existing", "proposals"],
        "meta_total": "created",
    },
    {
        "name": "action-proposals",
        "description": "List Atlas action proposals.",
        "inputs": {
            "--status": {
                "type": "str",
                "required": False,
                "default": None,
                "allowed": ["candidate", "approved", "rejected", "exported", "failed"],
            },
            "--destination": {"type": "str", "required": False, "default": None},
            "--limit": {"type": "int", "required": False, "default": 100},
        },
        "data_keys": "list[ActionProposal]",
        "meta_total": "len(data)",
    },
    {
        "name": "resolve-action-proposal",
        "description": "Update an Atlas action proposal status.",
        "inputs": {
            "--proposal-id": {"type": "int", "required": True},
            "--status": {
                "type": "str",
                "required": True,
                "allowed": ["candidate", "approved", "rejected", "exported", "failed"],
            },
            "--external-ref": {"type": "str", "required": False, "default": None},
        },
        "data_keys": "ActionProposal",
        "meta_total": "1",
    },
    {
        "name": "label-source-item",
        "description": "Set sensitivity label on an Atlas source_item. 'clear' resets to NULL.",
        "inputs": {
            "--source-item-id": {"type": "int", "required": True},
            "--sensitivity": {
                "type": "str",
                "required": True,
                "allowed": ["none", "low", "medium", "high", "redacted", "clear"],
            },
        },
        "data_keys": "SourceItem",
        "meta_total": "1",
        "note": "Records a source_import event (details='source_item_sensitivity_set') when the value changes; no-op records nothing. Re-importing a labeled source_item via import-whatsapp PRESERVES the label unless the importer explicitly passes a new sensitivity.",
    },
    {
        "name": "label-evidence-item",
        "description": "Set sensitivity label on an Atlas evidence_item. 'clear' resets to NULL.",
        "inputs": {
            "--evidence-item-id": {"type": "int", "required": True},
            "--sensitivity": {
                "type": "str",
                "required": True,
                "allowed": ["none", "low", "medium", "high", "redacted", "clear"],
            },
        },
        "data_keys": "EvidenceItem",
        "meta_total": "1",
        "note": "Records a media_process event (details='evidence_item_sensitivity_set') when the value changes; no-op records nothing.",
    },
    {
        "name": "enqueue-media-retry",
        "description": "Enqueue a media-retry row. LifeAgent/wacli calls this when a WhatsApp media is missing or fetch failed. Idempotent on (source_item_id, media_key).",
        "inputs": {
            "--source-item-id": {"type": "int", "required": True},
            "--media-key": {"type": "str", "required": True, "note": "External media identifier (wacli message id)."},
            "--chat-id": {"type": "str", "required": False},
            "--media-type": {"type": "str", "required": False, "note": "audio|image|document|video"},
            "--media-path": {"type": "str", "required": False, "note": "Local path if partially downloaded."},
            "--media-url": {"type": "str", "required": False, "note": "Remote URL hint."},
            "--next-attempt-time": {"type": "str", "required": False, "note": "ISO-8601; row stays 'pending' until this passes."},
        },
        "data_keys": "MediaRetryItem",
        "meta_total": "1",
    },
    {
        "name": "process-media-retry-queue",
        "description": "Claim up to N pending media-retry rows whose next_attempt_time has passed. Transitions pending -> retrying, increments attempt_count, returns claimed rows + queue counts. LifeAgent then fetches each via wacli and reports back via record-media-retry-outcome.",
        "inputs": {
            "--limit": {"type": "int", "required": False, "default": 25, "min": 1},
        },
        "data_keys": ["attempted", "expired", "recovered", "failed", "pending_remaining", "rows"],
        "meta_total": "attempted",
    },
    {
        "name": "record-media-retry-outcome",
        "description": "Record LifeAgent's fetch result for a media-retry row. 'done' requires --media-path. 'expired' is terminal (HTTP 403/410). Records audit event with from/to status.",
        "inputs": {
            "--retry-id": {"type": "int", "required": True},
            "--status": {
                "type": "str",
                "required": True,
                "allowed": ["pending", "retrying", "expired", "done", "failed"],
            },
            "--media-path": {"type": "str", "required": False, "note": "Required when status='done'."},
            "--last-http-status": {"type": "int", "required": False},
            "--last-error": {"type": "str", "required": False},
            "--next-attempt-time": {"type": "str", "required": False},
        },
        "data_keys": "MediaRetryItem",
        "meta_total": "1",
    },
    {
        "name": "transcribe-source-item",
        "description": "Run transcription on a source_item via the chosen provider; stores transcript as an evidence_item. Mock is default. Real provider 'openai' uses Whisper API via OPENAI_API_KEY + OPENAI_BASE_URL.",
        "inputs": {
            "--source-item-id": {"type": "int", "required": True},
            "--provider": {"type": "str", "required": False, "default": "mock", "allowed": ["mock", "openai"]},
        },
        "data_keys": ["source_item_id", "created", "evidence", "error", "provider"],
        "meta_total": "1 if evidence else 0",
        "note": "Provider failures are recorded as media_process events with details='media_process_failed'; the source_item is preserved.",
    },
    {
        "name": "ocr-source-item",
        "description": "Run OCR on a source_item via the chosen provider; stores OCR text as an evidence_item. Mock is default. Real provider 'tesseract' requires the pytesseract Python package AND a system 'tesseract' binary on PATH.",
        "inputs": {
            "--source-item-id": {"type": "int", "required": True},
            "--provider": {"type": "str", "required": False, "default": "mock", "allowed": ["mock", "tesseract"]},
        },
        "data_keys": ["source_item_id", "created", "evidence", "error", "provider"],
        "meta_total": "1 if evidence else 0",
        "note": "Provider failures are recorded as media_process events with details='media_process_failed'.",
    },
    {
        "name": "list-media-retries",
        "description": "List media-retry rows; filter by status and/or source_item_id.",
        "inputs": {
            "--status": {"type": "str", "required": False, "allowed": ["pending", "retrying", "expired", "done", "failed"]},
            "--source-item-id": {"type": "int", "required": False},
            "--limit": {"type": "int", "required": False, "default": 100},
        },
        "data_keys": "list[MediaRetryItem]",
        "meta_total": "len(data)",
    },
    {
        "name": "edit-action-proposal",
        "description": "Edit user-facing fields of an Atlas action proposal (title/description/due/confidence). Status/external_ref/exported_at are NOT editable here — use resolve-action-proposal for those.",
        "inputs": {
            "--proposal-id": {"type": "int", "required": True},
            "--title": {"type": "str", "required": False, "default": None, "note": "Non-blank if provided."},
            "--description": {"type": "str", "required": False, "default": None},
            "--suggested-due-at": {"type": "str", "required": False, "default": None, "note": "ISO-8601."},
            "--confidence": {"type": "float", "required": False, "default": None, "min": 0.0, "max": 1.0},
        },
        "data_keys": "ActionProposal",
        "meta_total": "1",
        "note": "Records an action_proposal event with details='action_proposal_fields_updated' and a payload listing the changed field names. No-op (no fields differ from current) does NOT record an event.",
    },
    {
        "name": "export-actions",
        "description": "Export approved Atlas action proposals to a Super-Productivity bridge JSON file.",
        "inputs": {
            "--output": {"type": "path", "required": True},
            "--destination": {"type": "str", "required": False, "default": "super-productivity"},
            "--limit": {"type": "int", "required": False, "default": 100},
            "--dry-run": {"type": "bool", "required": False, "default": False},
        },
        "data_keys": ["destination", "output_path", "exported", "proposal_ids"],
        "meta_total": "exported",
    },
    {
        "name": "atlas-version",
        "description": "Print the Atlas API/CLI contract version + spec.",
        "inputs": {},
        "data_keys": [
            "atlas_contract_version",
            "atlas_contract_name",
            "subcommands",
            "endpoints",
            "breaking_changes_since",
        ],
        "meta_total": "1",
    },
]


ATLAS_ENDPOINTS: list[dict[str, Any]] = [
    {
        "method": "GET",
        "path": "/api/action-proposals",
        "description": "List Atlas action proposals (filter by status/destination).",
        "query": {
            "status": {"type": "str", "required": False, "default": None},
            "destination": {"type": "str", "required": False, "default": None},
            "limit": {"type": "int", "required": False, "default": 100, "min": 1, "max": 500},
        },
        "response_keys": ["ok", "rows", "proposals"],
    },
    {
        "method": "POST",
        "path": "/api/action-proposals/status",
        "description": "Update an Atlas action proposal status (approve/reject/export).",
        "request": {
            "proposal_id": {"type": "int", "required": True, "min": 1},
            "status": {
                "type": "str",
                "required": True,
                "allowed": ["candidate", "approved", "rejected", "exported", "failed"],
            },
            "external_ref": {"type": "str", "required": False, "default": None},
        },
        "response_keys": ["ok", "proposal"],
    },
    {
        "method": "GET",
        "path": "/api/atlas/version",
        "description": "Atlas contract version + full spec; consumers should refuse to start on major mismatch.",
        "query": {},
        "response_keys": [
            "ok",
            "atlas_contract_version",
            "atlas_contract_name",
            "subcommands",
            "endpoints",
            "breaking_changes_since",
        ],
    },
]


BREAKING_CHANGES_SINCE: list[dict[str, str]] = []
"""History of breaking changes. Each entry: {"version": "X.Y.Z", "summary": "...", "date": "YYYY-MM-DD"}.

Empty in 1.0.0 — the contract was born here. Future major bumps must append.
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def atlas_meta(subcommand: str) -> dict[str, Any]:
    """Return the contract-version meta block to inject into Atlas envelopes.

    Atlas CLI handlers pass this into ``_json_envelope(extra_meta=...)`` so
    consumers see ``meta.atlas_contract_version`` and ``meta.atlas_subcommand``.
    """
    return {
        "atlas_contract_version": ATLAS_CONTRACT_VERSION,
        "atlas_subcommand": subcommand,
    }


def atlas_contract_payload() -> dict[str, Any]:
    """Return the full Atlas contract spec.

    Used by both the ``atlas-version`` CLI subcommand and the
    ``GET /api/atlas/version`` dashboard endpoint. Stable shape — adding a top
    level key is a minor bump, removing or renaming one is a major bump.
    """
    return {
        "atlas_contract_version": ATLAS_CONTRACT_VERSION,
        "atlas_contract_name": ATLAS_CONTRACT_NAME,
        "subcommands": ATLAS_SUBCOMMANDS,
        "endpoints": ATLAS_ENDPOINTS,
        "breaking_changes_since": BREAKING_CHANGES_SINCE,
    }


__all__ = [
    "ATLAS_CONTRACT_VERSION",
    "ATLAS_CONTRACT_NAME",
    "ATLAS_SUBCOMMANDS",
    "ATLAS_ENDPOINTS",
    "BREAKING_CHANGES_SINCE",
    "atlas_meta",
    "atlas_contract_payload",
]

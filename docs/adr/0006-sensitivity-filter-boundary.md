# 0006 Sensitivity Filter Boundary

Date: 2026-05-05

Status: Accepted

Source Claims: claim #12855, claim #32380, claim #36321

## Context

MemoryMaster has a canonical sensitivity filter for claim ingestion. Atlas Inbox also stores user-selected source content such as WhatsApp messages and media transcripts. Applying the same redaction rules everywhere can destroy legitimate imported content, but skipping filtering for claims would allow sensitive material to become long-lived memory.

## Decision

Claim ingestion must obey the INGEST filter rules even if user instructions conflict. The filter is a storage boundary, not just a transport boundary.

Atlas `source_items` and `evidence_items` intentionally do not route user-imported text through `security.redact_text`. Those tables store explicit user-chosen source material. Claims extracted from that source material still flow through `service.ingest()`, including sanitization and sensitivity checks, before they land in the claims table.

## Consequences

Raw source fidelity is preserved for explicit imports.

Long-lived claims remain protected by the canonical ingest filter.

Future audits should not add blanket redaction to source or evidence writes without revisiting this boundary.

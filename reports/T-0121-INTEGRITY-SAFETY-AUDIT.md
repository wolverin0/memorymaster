<!-- doc-head: T-0121 read-only claims sanitization and support-lineage audit -->
Audits the authoritative SQLite claims corpus for credential, private-network, personal-path, and raw-code residue.
Checks generic, compiled-profile, entity-graph, and graph-observation supports for duplicates, orphans, and lifecycle violations.
Read this before any manual curation; it reports IDs and counts only and performs no database mutation.
<!-- /doc-head -->

# T-0121 integrity safety audit

Observed 2026-08-13. Verdict: **FAIL sanitization; PASS structural lineage**.

## Scope and method

The authoritative SQLite database was opened with URI read-only mode. All 128,528 claims were scanned across `text`, `subject`, `predicate`, `object_value`, and `scope`. No matched value was printed or persisted; examples below are claim IDs only. The exact machine-readable evidence is in [t0121-integrity-safety-evidence.json](t0121-integrity-safety-evidence.json:1).

Credential detection used the production decoded-variant scanner, which normalizes, decodes supported encodings, and returns finding labels without returning the secret. See `memorymaster/core/security.py:450-491`. Private IP, personal-path, fenced-code, and probable multiline-code checks were separate because bare private IPs are intentionally excluded from the ingest credential filter; the policy is documented at `memorymaster/core/security.py:55-60`.

This is the safety subset only. Retrieval quality, memory usefulness, duplicates by meaning, contradiction quality, and curation decisions remain outside T-0121.

## Finding S-01 — stored sanitization residue

Severity: High. Exploitability: `EXPLOITABLE-LOW-EFFORT` for a process or operator already able to read the local database; no remote exploit path was evaluated.

The required zero-residue criterion is not met:

| Category | All stored | Confirmed | Other lifecycle states | Example human IDs |
|---|---:|---:|---|---|
| Credential or token detector | 65 | 0 | 48 archived, 6 stale, 11 superseded | `mm-4937`, `mm-9998`, `mm-c956`, `mm-88a3`, `mm-ee8d` |
| Bare private IP | 306 | 46 | 85 archived, 54 conflicted, 44 stale, 77 superseded | `mm-630f`, `mm-bccb`, `mm-56a6~2` |
| Personal path | 537 | 82 | 145 archived, 10 conflicted, 266 stale, 34 superseded | `mm-ef2a`, `mm-a2ba~2`, `mm-1a73` |
| Fenced code | 1,112 | 7 | 960 archived, 1 conflicted, 82 stale, 62 superseded | `mm-bc6f`, `mm-e1af`, `mm-1fb8~2` |
| Probable multiline code | 46 | 0 | 43 archived, 2 stale, 1 superseded | none confirmed |

Evidence: `reports/t0121-integrity-safety-evidence.json:9-49`.

The 65 credential/token findings are not confirmed claims, which limits ordinary trusted recall exposure, but they remain at rest and are labeled `public`, not `sensitive`. Redacted review of representative rows showed detector classes including credential assignments, embedded database passwords, SSH password flags, token-shaped values, and a Telegram bot-token shape. Current validity was not tested, so live credential usability is `UNKNOWN`.

The current ingestion path does reject or classify sensitive claim content before graph extraction: `memorymaster/knowledge/entity_graph.py:182-199`. This finding is corpus residue, not evidence that the current graph-observation synthesizer accepted sensitive support.

## Finding I-01 — support lineage is structurally clean

Severity: Informational. Exploitability: `BAD-PRACTICE` does not apply; no defect was found.

All checked counts were zero:

- SQLite foreign-key violations.
- Exact duplicate groups in `claim_evidence_links`, `claim_links`, `compiled_profile_supports`, `entity_edge_supports`, and `graph_observation_supports`.
- Orphans from those supports to claims, evidence, source items, entities, compiled facts, verbatim memories, entity edges, or graph observations.
- Entity-graph supports backed by non-confirmed, sensitive, observation-generated, or cross-claim-scope claims.
- Compiled-profile support session mismatches.

Evidence: `reports/t0121-integrity-safety-evidence.json:52-99`.

Graph-observation-specific support tables currently contain zero observations and zero support rows, so their orphan result is a valid empty-state check—not proof of live observation lineage. Entity-edge support has 268 rows, all attached to confirmed, non-sensitive, non-observation claims with exact claim/support scope.

Entity records can retain the scope in which a canonical alias was first created; this is not counted as a lineage mismatch because the registry intentionally resolves an existing alias before creating a scoped entity (`memorymaster/knowledge/entity_registry.py:196-257`). Observation discovery partitions on the support row's exact scope and tenant (`memorymaster/knowledge/graph_observations.py:237-263`).

## Disposition

No claim, citation, support, event, or database row was deleted, rewritten, archived, or reclassified. This report and its count-only evidence artifact are the only changes.

Recommended next action: create a separately authorized, dry-run-first remediation task. Review the 65 inactive credential/token hits first, then decide policy for confirmed private IPs, personal paths, and fenced code. Do not bulk-redact from pattern matches alone.

[SECTION COMPLETE: T-0121 safety subset]

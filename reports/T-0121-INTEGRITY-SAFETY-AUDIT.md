<!-- doc-head: T-0121 read-only claims sanitization and support-lineage audit -->
Audits the authoritative SQLite claims corpus for credential, private-network, personal-path, and raw-code residue.
Checks generic, compiled-profile, entity-graph, and graph-observation supports for duplicates, orphans, and lifecycle violations.
Read this before any manual curation; it reports IDs and counts only and performs no database mutation.
<!-- /doc-head -->

# T-0121 integrity safety audit

Observed 2026-08-13. Verdict: **FAIL sanitization; PASS structural lineage**. The failure is split: credentials and fenced code are historical, while private-IP and personal-path intake is ongoing.

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

The 65 credential/token detector hits are not confirmed claims, which limits ordinary trusted recall exposure, but they remain at rest and are labeled `public`, not `sensitive`. They are **not 65 confirmed secrets**.

Redacted manual context review classified the 65 as:

| Triage class | Count | Meaning |
|---|---:|---|
| Credential-bearing context | 20 | Historical context contains credential material; current validity was not tested |
| Non-secret context | 36 | False positive or a fixture, variable name, identifier, code example, or secret-file reference |
| Unresolved | 9 | Redaction removed too much context for a truthful classification |

The live-secret count remains unknown. Evidence and exact human-ID sets: `reports/t0121-integrity-safety-evidence.json:86-122`.

Value-safe identity grouping found **18 distinct credential identities across the 20 credential-bearing rows**. Exact comparison with the three previously accepted-risk credentials cannot be established safely because that decision supplies provider categories, not value fingerprints. The defensible result is therefore 0-3 possible matches and **15-18 different identities**. Context labels alone associate six identities with Supabase, none with the DashScope family, and twelve with neither; those labels are not identity proof. Values and hashes were neither emitted nor persisted.

The current ingestion path does reject or classify sensitive claim content before graph extraction: `memorymaster/knowledge/entity_graph.py:182-199`. This finding is corpus residue, not evidence that the current graph-observation synthesizer accepted sensitive support.

## Finding S-02 — recency separates historical residue from ongoing intake

Severity: Medium. Exploitability: `BAD-PRACTICE`; this is a governance/intake-policy gap, not a demonstrated remote exploit.

The 30-day window uses immutable `claims.created_at`, from 2026-07-14 20:05 UTC through 2026-08-13 20:05 UTC:

| Category | Last 30 days | Confirmed in window | Latest hit |
|---|---:|---:|---|
| Credential or token detector | 0 | 0 | 2026-04-22 |
| Bare private IP | 61 | 22 | 2026-08-13 |
| Personal path | 50 | 32 | 2026-08-12 |
| Fenced code | 0 | 0 | 2026-05-12 |

This makes credential and fenced-code residue historical. Private-IP and personal-path intake is ongoing and includes claims served by trusted recall.

Recent private-IP hits by source agent: `claude-session=45`, `dream-worker=9`, `llm-stop-hook=4`, `atlas-llm-extractor=2`, `codex-session=1`. Recent personal-path hits: `claude-session=46`, `codex-session=2`, `dream-worker=1`, `atlas-llm-extractor=1`.

For 2026-08-13 in Argentina, `claude-session` created 67 claims. It produced zero credential/token, personal-path, or fenced-code hits. It produced one private-IP hit, `mm-b33d`, currently conflicted. Value-redacted review confirms this is an actual NFS topology address, not a repo-relative-path false positive.

Evidence: `reports/t0121-integrity-safety-evidence.json:52-83`. The production scanner intentionally excludes bare private IPv4 at ingest (`memorymaster/core/security.py:55-60`), which explains how the current operator rule can be violated without triggering the shared credential detector.

## Finding I-01 — support lineage is structurally clean

Severity: Informational. Exploitability: `BAD-PRACTICE` does not apply; no defect was found.

All checked counts were zero:

- SQLite foreign-key violations.
- Exact duplicate groups in `claim_evidence_links`, `claim_links`, `compiled_profile_supports`, `entity_edge_supports`, and `graph_observation_supports`.
- Orphans from those supports to claims, evidence, source items, entities, compiled facts, verbatim memories, entity edges, or graph observations.
- Entity-graph supports backed by non-confirmed, sensitive, observation-generated, or cross-claim-scope claims.
- Compiled-profile support session mismatches.

Evidence: `reports/t0121-integrity-safety-evidence.json:124-170`.

Graph-observation-specific support tables currently contain zero observations and zero support rows, so their orphan result is a valid empty-state check—not proof of live observation lineage. Entity-edge support has 268 rows, all attached to confirmed, non-sensitive, non-observation claims with exact claim/support scope.

Entity records can retain the scope in which a canonical alias was first created; this is not counted as a lineage mismatch because the registry intentionally resolves an existing alias before creating a scoped entity (`memorymaster/knowledge/entity_registry.py:196-257`). Observation discovery partitions on the support row's exact scope and tenant (`memorymaster/knowledge/graph_observations.py:237-263`).

## Disposition

No claim, citation, support, event, or database row was deleted, rewritten, archived, or reclassified. This report and its count-only evidence artifact are the only changes.

Remediation remains operator-gated. The proposed sequence is:

1. Fix intake before cleanup through T-0127: reject bare private IPs and absolute personal paths on every claim-ingest surface while allowing repo-relative paths.
2. Add positive and negative enforcement tests for bare private IP, absolute personal path, repo-relative path, and fenced code.
3. Have a value-authorized human resolve the nine indeterminate credential hits; never convert the 20 credential-bearing contexts into “live secrets” without validity evidence.
4. Only after explicit operator approval, produce a no-write dry-run manifest separated into active served rows and historical at-rest rows. No bulk action should be inferred from detector matches.

[SECTION COMPLETE: T-0121 safety subset]

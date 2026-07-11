# Phase 0 Adversarial Red-Test Matrix

All tests use temporary databases, isolated environment variables, fake providers, and local/fake services unless explicitly marked external. Unsafe current behavior should be committed as `xfail(strict=True)` tests and demonstrated with `--runxfail`; fixes remove the marker.

## Shared fixtures

- `isolated_mm_env`: clears inherited DB/Qdrant/provider/auth settings and redirects state/spool/snapshots to `tmp_path`.
- `policy_db`: claims in every lifecycle state across tenants/projects.
- `synthetic_secret`: deterministic non-credential test token.
- `durable_payload_scan`: scans all durable string/JSON fields for a fixture.
- `db_fingerprint`: proves denied operations cause no domain mutation.
- `FakeQdrant`: returns caller-controlled IDs/payloads without network/model dependencies.
- `rendered_hook`: renders hook templates into isolated state directories.

## Matrix

| Finding | Test file | Required red tests | External dependency |
|---|---|---|---|
| MM-SEC-01 | `tests/test_mcp_authorization_boundary.py` | reader cannot ingest with spoofed source; unknown team principal fails closed; scope allowlist cannot expand context; list/query/pin/redact cannot cross project/tenant; every MCP tool declares an action | Real Postgres/RLS subset requires DSN |
| MM-SEC-02 | `tests/test_qdrant_authoritative_filtering.py`; `tests/test_qdrant_retrieval_quarantine.py`; `tests/test_verbatim_qdrant_quarantine.py` | never return orphan payload; filter archived/candidate/stale/conflicted/wrong-scope/wrong-tenant/sensitive/private; payload cannot override DB; safe lexical/FTS fallback; direct adapters and disabled CLI fail before model/network/backend access; equal-count/different-ID reconcile | Real authenticated/TLS Qdrant final parity (`BLOCKED-EXTERNAL`) |
| MM-SEC-03 | `tests/test_persisted_envelope_sensitivity.py` | plain and encoded secret matrix over every claim/citation/provenance field; legacy sensitive metadata hidden from list/query/export/Qdrant | None |
| MM-SEC-04 | `tests/test_write_gateway_paths.py` | compact-summary, steward existing-row update, verbatim/spool/Atlas/miner/import paths reject secret fixture | None |
| MM-ARCH-01 | `tests/test_entity_schema_composition.py` | normal init then graph schema; registry-first extract/stats/related; read tools issue no DDL | None |
| MM-ARCH-02 | `tests/test_retrieval_surface_parity.py` | conversational vs keyword IDs across MCP/context/hook/CLI; trusted defaults exclude provisional statuses | ML parity may require model |
| MM-REL-02 | `tests/test_mcp_read_only_contract.py` | query succeeds under held write lock; unchanged access count; one aggregated spool signal; one retrieval per detail level | None |
| MM-OPS-01/02/04 | `tests/test_deployment_contracts.py` | required secret interpolation; private backend ports; matching entrypoint/health; Helm probes; pinned images | Built-runtime/Kubernetes final checks |
| MM-UX-01 | `tests/test_setup_profile_verification.py` | requested component failure returns nonzero/PARTIAL; provider/MCP/hook/vector checks are independently reported | Docker/provider optional cases |
| MM-COST-01/02 | `tests/test_stop_hook_capture_policy.py` | default quiet/nonblocking; only appended lines processed; persisted budget survives restart; finite defaults | None |
| MM-DEMO-01 | `tests/test_atlas_mock_evidence_guard.py` | missing provider fails without evidence; explicit mock requires dev gate; mock evidence cannot feed claims/actions | None |
| MM-LIFE-01 | `tests/test_scheduled_archive_lifecycle.py` | scheduled archive increments version, timestamps, event, cache/vector/outbox; template contains no direct status SQL | None |

## Existing tests requiring semantic updates

- Unknown/None principal default-writer tests become explicit local-profile behavior only.
- Tenantless visibility/pin tests become explicit local-profile behavior only.
- Unlimited budget behavior becomes an explicitly unsafe legacy/profile test, not the default.
- Atlas mock-default tests require a dev gate.
- Direct maximum-capture Stop-hook tests require an explicit flag.
- Count-only Qdrant reconciliation tests are replaced by set/content convergence.

## External-only gates

- Postgres RLS and application-role enforcement.
- Built container readiness, network exposure, and MCP handshake.
- Kubernetes scheduling/readiness/network policy.
- Real Qdrant auth/TLS/reconciliation.
- ML/hybrid parity when required models are unavailable.

These become `BLOCKED-EXTERNAL` with owner/evidence requirements when unavailable; static/fake-backed tests remain mandatory in normal CI.

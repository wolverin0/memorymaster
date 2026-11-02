# AUDIT-T2070 — security audit and tests

- **Date:** 2026-11-02
- **Base:** `origin/main` at `d0997c5`
- **Branch:** `claude/T2070-audit-memorymaster`
- **Scope:** injection, authz/ownership, secrets in tree, unsafe deserialization,
  SSRF, dependency advisories. The audit commit changed no product code; the
  follow-up fix for F-1a/F-1b (see "Fix status") touches the dashboard and the shared review-queue builder.

Severity scale: **High** = a lower-privileged caller reads or changes data the
design says it must not; **Medium** = real exposure that needs a non-default
config or an already-trusted caller; **Low** = defence-in-depth gap;
**Info** = noted, no action needed.

## Findings

| ID | Severity | Location | Summary | Evidence |
|---|---|---|---|---|
| F-1a | **High** (token mode) / Medium (legacy loopback) | `memorymaster/surfaces/dashboard_read_models.py:42-44`, reached from `memorymaster/surfaces/dashboard.py:944` | `/api/claims` hands `visibility="sensitive"` claims from **any scope and any author** to the **viewer** role. `claims_payload` calls `service.list_claims()` with no `scope_allowlist` and no `requesting_agent`, and its only filter, `is_sensitive_claim()` (`core/security.py:810`), scans text for secret patterns and never reads `claim.visibility`. MCP `list_claims` (`mcp_server.py:2508`) passes the workspace scope allowlist and the request principal, so the same claim is hidden there. | Reproduced live: a viewer-token GET with default params returned the seeded sensitive claim. Fixed; regression test: `test_viewer_never_receives_non_public_claims` |
| F-1b | **High** (token mode) / Medium | `memorymaster/surfaces/dashboard.py:369-373`, `:917` | `/claim/<id>/lineage` loads any claim by id through `store.get_claim()` and renders its title/text with no visibility, scope, tenant or sensitivity check. Ids are sequential, so a viewer can walk them all. | Reproduced live. Fixed; regression test: `test_viewer_lineage_of_non_public_claim_is_404_without_content` |
| F-1c | Medium | `memorymaster/surfaces/dashboard_read_models.py:43`; `dashboard.py:948`, `:992` | Dashboard `allow_sensitive=1` query flag widens results directly. It skips `resolve_allow_sensitive_access()` (`core/security.py:280`), which is the env/config gate (`MEMORYMASTER_ALLOW_SENSITIVE_BYPASS`) that MCP and the service layer enforce. Any viewer can set it. Stored secrets are already redacted at ingest, so this mostly exposes redacted rows and the existence of sensitive claims, not raw values. | Defect record: `test_claims_payload_allow_sensitive_requires_bypass_gate` |
| F-2 | Medium | `pyproject.toml:40` (`capture` extra: `pypdf>=6.21.2`), used by `memorymaster/capture/adapters.py:410` | The declared floor admits 7 known pypdf advisories: CVE-2026-119203…119209, fixed in 6.21.3 and 6.21.4. The capture path parses inbox PDFs, so a malformed PDF reaches the vulnerable parser. Recommendation: raise the floor to `pypdf>=6.21.4`. I did not make that change here because it is a dependency/behaviour change outside this audit's scope. | `pip-audit --no-deps -r <floors>` against every declared minimum. Only pypdf was flagged. |
| F-3 | Medium | `memorymaster/surfaces/mcp_path_policy.py:94`, `:125` | The MCP `db`/`workspace` allowlist fails **open**: with `MEMORYMASTER_MCP_DB_ALLOWLIST` unset (the default), a stdio or local-trusted HTTP caller can point any tool at any SQLite path the process can write, including creating new files. This is documented back-compat. Team mode is not affected: `_normalize_team_arguments` (`mcp_server.py:937`) pins db/workspace/scope to the authenticated context. Also, `fnmatch` `*` matches `/` (`:77`), so `/a/*/memorymaster.db` also allows deeper paths. | Code reading; existing coverage in `tests/test_mcp_path_policy.py` |
| F-4 | Low | `memorymaster/surfaces/mcp_server.py:2924-2962` | The MCP tool `open_dashboard` connects to a **caller-supplied host:port**, returns the parsed JSON body as `health_payload`, and returns `str(exc)`. That makes it an internal-network probe and port-scan oracle (refused/timeout/reset can be told apart). The request is fixed to `GET /health`. The tool is off in team mode (`team_enabled=False`, `mcp_server.py:881`), so only the stdio or local-trusted bearer-token holder can reach it. | Defect record: `test_open_dashboard_refuses_non_loopback_host`; lock: `test_open_dashboard_only_issues_get_health` |
| F-5 | Low | `memorymaster/govern/steward_classifier.py:99` | `joblib.load()` (pickle) of the classifier artifact runs arbitrary code if that file is attacker-writable. Loading is opt-in: it needs `MEMORYMASTER_STEWARD_CLASSIFIER_ENABLED` or `..._PATH`. Setting only the PATH variable also enables it. The default path is the relative `artifacts/steward-classifier-v2.joblib`, so it resolves against the CWD. Recommendation: use an absolute path and check the file's owner and permissions before loading. | Lock: `test_classifier_never_unpickles_without_opt_in` |
| F-6 | Low | `memorymaster/surfaces/dashboard.py:870-874` | `_read_json_body` trusts `Content-Length` with no upper bound, so one authenticated POST can force a large read into memory. | Code reading |
| F-7 | Low | `memorymaster/surfaces/dashboard.py:840`, `:868` | 500 responses echo `str(exc)` to the client, which can leak internal paths and SQL errors. | Code reading |
| F-8 | Info | `scripts/probe_m2_continuity.py:52` | A constant named `SECRET` holds a **GitHub-PAT-shaped string**. It looks like a synthetic canary used to verify redaction in the M2 probe. **The operator should confirm it is not a live token.** Other secret-shaped hits are deliberate adversarial fixtures under `tests/` (for example `tests/fixtures/sensitivity_adversarial*.jsonl`, `tests/test_security_patterns.py`). `.env.example` is tracked; per the audit rules it was not opened. No private keys or other credential files are tracked. | Pattern scan over `git ls-files`; values masked, never printed |
| F-9 | Info | `memorymaster/surfaces/hook_probe.py:80`, `:96` | `exec()`/`runpy` run hook templates for `hooks doctor`. The paths come from the operator's own install, and this is by design. | Code reading |

### Checked and clean

- **SQL injection:** I reviewed every f-string or concatenated `execute()`: about 155 sites in `memorymaster/`. All interpolated pieces are internal constants: table or column names from fixed tuples, placeholder lists, `int()`-cast PRAGMAs, and clause lists built from fixed fragments with `?`/`%s` parameters (for example `_storage_sources.py:1122`, `workflow_intelligence/storage.py:292` with its `TABLES` allowlist). I found no user-controlled identifier.
- **Command injection:** none of the 36 `subprocess` call sites uses `shell=True`.
- **Dashboard auth:** constant-time token compare, Host-header (DNS-rebinding) check, Origin/Referer CSRF check, and non-loopback bind refusal without a token (`dashboard_auth.py`). All POST routes require the operator role, including the two that are missing from `POST_OPERATOR_ROUTES`.
- **MCP HTTP:** a bearer token is required at startup, compared with `hmac.compare_digest` (`mcp_http.py:55`). The host allowlist defaults to loopback, `hook_recall` has a body cap, and only the health routes skip auth.
- **Webhook SSRF:** non-http(s) schemes are refused before dispatch (`core/webhook.py:79-82`). This had no test until now.
- **Team-mode MCP:** db/workspace/scope/source_agent are forced to the authenticated context, and tools are allowlisted per policy (`mcp_server.py:858-937`).
- **Dependency floors:** apart from pypdf, every declared minimum audited clean. pip-audit also flagged pypdf 6.17, setuptools and wheel in the container's *installed* environment. That is staleness in this sandbox, not a repo finding, and is excluded.

## Tests added

All tests are in `tests/test_t2070_security_audit.py` and use `tmp_path` databases only. There are two kinds:

**Regression locks.** These cover guards that work today but had no direct test, and must keep passing:

- `test_viewer_never_receives_non_public_claims` (x19 routes), `test_viewer_still_sees_public_claims`, `test_operator_and_legacy_mode_still_see_non_public_claims`, `test_viewer_lineage_of_non_public_claim_is_404_without_content`, `test_viewer_lineage_of_public_claim_omits_non_public_neighbours`, `test_lineage_refuses_claim_of_another_tenant`: the F-1a/F-1b regression tests. They were `xfail` defect records when the audit was written and are plain tests since the fix. The seed now covers every claim state a route reads (candidate, conflicted+pinned, confirmed, superseded chains); the original single `candidate` seed let `/api/retrieval`, `/api/conflicts` and `/api/review-queue` pass for the wrong reason.
- `test_viewer_cannot_open_operator_stream`: the viewer gets 403 on the operator SSE route.
- `test_viewer_cannot_post_to_routes_missing_from_operator_route_list` (×2): POSTs to routes absent from `POST_OPERATOR_ROUTES` are still operator-only.
- `test_dashboard_rejects_foreign_host_header`: the DNS-rebinding guard works.
- `test_operator_post_with_foreign_origin_is_csrf_rejected`: the CSRF check holds even when the token is valid.
- `test_lineage_page_escapes_claim_html`: `<script>`, `<img onerror>` and `<b>` in a claim's text, subject and object are escaped.
- `test_claims_payload_default_filters_redacted_claims`
- `test_webhook_refuses_non_http_schemes_without_dispatch` (×5: file, FILE, ftp, gopher, data) plus a positive control, `test_webhook_http_scheme_is_dispatched`.
- `test_open_dashboard_only_issues_get_health`: whatever `path` the caller passes, the probe sends exactly `GET /health`.
- `test_classifier_never_unpickles_without_opt_in`: `joblib.load` is not called on a file at the default path unless loading is enabled. It skips if `joblib` is not installed.

**Defect records**, marked `xfail(strict=True)`. Each one asserts the *secure* behaviour for a confirmed finding. They xfail today. When someone fixes the defect, the test XPASSes, strict mode turns that into a failure, and that failure is the cue to remove the marker:

- `test_claims_payload_allow_sensitive_requires_bypass_gate` (F-1c)
- `test_open_dashboard_refuses_non_loopback_host` (F-4)

I ran both remaining records with `--runxfail`, and each fails on its assertion, not on a setup error.

## How to run

```bash
pip install -e ".[dev,mcp]" joblib              # joblib only for the classifier lock
python -m pytest tests/test_public_demo.py -q   # disposable lifecycle first (AGENTS.md)
python -m pytest tests/test_t2070_security_audit.py -q -rxX
python -m pytest tests/test_t2070_security_audit.py -q --runxfail   # shows the 2 open defects failing
ruff check tests/test_t2070_security_audit.py
python scripts/bulk_check.py --fast             # fast merge gates; full suite: drop --fast
```

## Fix status (F-1a, F-1b)

Both reproduced on unpatched code (19 of the new tests failed before the fix, all pass after). Root cause: the
dashboard read paths called the service/store with no `requesting_agent`, so the per-agent visibility rule
(`_filter_agent_visibility`: public, or authored by the requester) never ran, and the lineage page loaded by id
through `store.get_claim` (no visibility, no tenant). The same gap existed on `/api/conflicts` (store-level read),
`/api/review-queue`, `/api/v1/review-queue` (raw SQL), `/api/retrieval`, `/api/recall-analysis` and `/api/namespaces`.

Fix: a token carries a role, not an identity. `dashboard_auth.requesting_agent_for()` maps the viewer role to the
non-author principal `dashboard:viewer`, `_enforce_auth` stores it per request, and every claim read passes it through
the existing visibility predicate. Operator and legacy (no-token) mode pass `None`, which is unchanged behaviour. An
unauthorized lineage id returns the same 404 page as a missing id. The lineage loader now also enforces `tenant_id`
for every role. Scope is deliberately not used as a viewer boundary: tokens have no scope grant and the dashboard is a
whole-database view.

Open: F-1c (`allow_sensitive` query flag) and F-4 remain defect records.

Results on this branch. Audit commit, before the fix:

- The new file: 20 passed, 4 xfailed.
- The new file plus the neighbouring suites (`test_public_demo`, `test_dashboard_auth`, `test_dashboard_lineage`, `test_webhook*`, `test_mcp_helpers`, `test_steward_classifier`, `test_mcp_path_policy`, `test_mcp_http_entrypoint`): 152 passed, 2 skipped, 4 xfailed.
- Ruff: clean.

After the fix (rebased on `origin/main`): 19 of the new tests failed on unpatched code. The PR file plus `test_dashboard*`, `test_review*`, `test_service_runtime_authority`, `test_security_patterns`, `test_public_demo`, `test_architecture_budgets`, `test_mcp_helpers`, `test_webhook*`, `test_mcp_path_policy` and `test_steward_classifier` give 356 passed, 1 skipped, 2 xfailed; ruff clean.

Known limit of the fix: `query_rows` runs `require_permission("dashboard:viewer", "query")`, which passes because unknown agents default to the WRITER role. A roles config that denies `query` to unknown agents would make the viewer fail on `/api/retrieval`. A public claim in another project's scope stays visible to a viewer.

The POST-rejection tests print a harmless `BrokenPipeError` from `socketserver` on stderr. That happens because the server answers 403 without draining the request body.

Review round (head after 032b7d9): non-public rows used to be dropped after the store applied its limit, so a flood of private claims could leave a viewer with fewer or zero public rows. `MemoryService.list_claims` (for a restricted caller), the review-queue builder, the conflict groups (which also gained the missing tenant filter) and the lineage candidate read now page with `page_authorized_lexical`, the same window-growing helper the lexical recall path uses. `/api/capture-inbox` lists derived claims by id and now applies the same rule. A missing or unknown role maps to the restricted principal (only the operator role is unrestricted), and the principal checks use `is not None`.

Review round 2: the policy now lives in one predicate, `authorize_claims` in `core/service.py` (tenant, then per-agent visibility, then sensitivity). `list_claims`, the review-queue gather and its flagged-claim slice (authorized before it counts toward the limit), the conflict peer read, the capture-inbox claim rows and the lineage reads all use it. `list_claims` always pages, so redacted rows cannot use up the window for unrestricted callers either. The operator's capture inbox and lineage page keep redacted claims (include_sensitive for the operator only); a restricted caller gets the full policy.

## What I did not check

- `/api/graph-observations` reads system-generated observation claims straight from SQL and is not covered by a test: seeding it needs the graph pipeline. `/api/provenance` returns per-agent counts (agent names included), not claim content. The raw capture evidence payloads on `/api/capture-inbox` are governed by source sensitivity, not by claim visibility, and were not changed.

- The PostgreSQL store and its RLS migrations against a live database. I only read them for SQL construction.
- Qdrant, Kuzu and the embedding providers at runtime.
- DOM-XSS in the dashboard template JavaScript (`dashboard_template.py`, `jev_dashboard`, `capture_inbox`). I only verified server-side escaping on the lineage page.
- Installed hook templates under `memorymaster/config_templates/` and the Windows task installer.
- The 36 `subprocess` sites beyond confirming that none uses `shell=True`; argument provenance was not traced per site.
- Outbound LLM, provider and Jev transport TLS settings (`decisions/transport.py`, `core/llm_provider.py`, `dreaming/providers.py`), apart from confirming their URLs come from operator configuration.
- Whether the token in F-8 is live. The value was deliberately not inspected beyond its shape.
- The full non-ML suite and `bulk_check.py` without `--fast`. See the PR for what ran.
- Mutating MCP tools (`forget`, `redact_claim_payload`, `pin_claim`, `resolve_steward_proposal`) in local, non-team mode: I did not trace whether they enforce tenant and scope ownership.

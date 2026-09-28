<!-- doc-head: independent read-only review of the 20260928 shared code-navigation setup -->
<!-- Covers: metric-denominator audit, gold-label/overflow/discovery integrity checks, Windows path
normalization, Serena source-body correspondence, source-vs-index guard, client smoke evidence
quality, and remaining limitations (graph callers, zero-based lines). -->
<!-- Read when: deciding whether to trust REPORT.md's numbers or acting on its PASS/FAIL verdicts. -->
<!-- /doc-head -->
# Independent review — shared code navigation (2026-09-28)

Reviewer: independent read-only worker. No implementation, config, cohort, or provider files were
modified. Verification performed by re-deriving numbers from the raw JSON artifacts and by running
the two local, already-existing preflight tests — no provider/tool calls were made.

## Verdict

**PASS overall.** Every headline number in `benchmark-summary.json` and `REPORT.md` was
independently re-derived from raw evidence and matched exactly. Methodology (gold-label
separation, overflow handling, path normalization, source-body correspondence, denominators) is
sound and honestly caveated. Two findings below are real but neither invalidates the reported
numbers; both are process/disclosure gaps, not data-integrity bugs.

## Checks performed and results

| Check | Result |
|---|---|
| Metric denominators | Confirmed. `n=20` throughout; exact-symbol subset correctly `n=5` (`serena_exact_discovery`, `exact_n`); payload-comparison subset correctly `n=19` (excludes the one overflow). rg 7/20, GitNexus 9/20 file@5, Serena exact 5/5, known-body 19/20 — all re-summed from `benchmark-rows.json`/`serena-exact-discovery.json` and matched `benchmark-summary.json` exactly. |
| No gold-label discovery claims | Confirmed. `benchmark.py`'s own `limitations` list and `cohort-review.md` explicitly flag that `serena_known_target` calls receive the gold `relative_path`/name (not a discovery metric); only the 5 `serena-exact-discovery.json` calls omit the path and are true discovery (5/5), and are labeled as such throughout. |
| Overflow not counted as success | Confirmed. `nav-017`'s Serena response is the literal "answer is too long (6367 characters)" overflow message; `summarize.py` correctly scores both `symbol_present` and `body_matches_live_source` as `False` for it (verified directly against `benchmark-rows.json`). It is excluded from the 19-row payload comparison. |
| Windows path normalization | Confirmed. `paths()` in `benchmark.py` and the cohort's `expected_path` both normalize to forward slashes; spot-checked `benchmark-rows.json` GitNexus `paths` arrays (e.g. `nav-002`, `nav-003`) — all forward-slash, matched correctly against `expected_path`. |
| Source-body correspondence | Confirmed. `summarize.py`'s `expected()` reconstructs the live-source slice from `body_location.start_line/end_line` (0-based list indices, consistent with zero-based `body_location`) and lstrips only the first line, matching the documented "not a whole-body dedent" behavior. Spot-checked `nav-006` (`CaptureRepository.lease_jobs`): returned body's first line is dedented, subsequent lines retain original 8-space indentation, matches file content. Re-summed `whole_file_chars=383880`, `symbol_response_chars=30477` from the 19 `body_matches_live_source=True` rows — exact match to `benchmark-summary.json`'s `0.9206080025007815` reduction figure. |
| Source-vs-index guard | Confirmed live. Ran `python scripts/check_code_navigation.py` against the actual (dirty) checkout: correctly returned `WARN`/`SOURCE_DIRTY_VERIFY_LIVE`, listed 30 changed/untracked source files including `memorymaster/capture/jev_shadow.py`, and correctly excluded `repos/`, `artifacts/`, `delta-exchange/` prefixes per `EXCLUDED`. Also ran the 4 disposable-repo tests in `tests/test_code_navigation_preflight.py` — **4 passed**. `ruff check` on the two new scripts plus the modified hook — **all checks passed**. |
| Client smoke — actual call counts / no denials | Confirmed. Codex's JSONL trace shows exactly 2 `mcp_tool_call` items (gitnexus `context`, serena `find_symbol`), both `"error":null,"status":"completed"`. Claude's final JSON has `permission_denials: []`. AGY's final JSON has no `denied_actions` field (present with a denial in its `.first-attempt` receipt, absent in the repaired run). Claim holds. |
| Client smoke ≠ savings claim | Confirmed appropriately caveated in both `docs/code-navigation.md` ("Tool payload reduction is different from total session token/cost savings") and `REPORT.md` ("These single smokes are connectivity evidence, not paired savings evidence... No claim of 92% cheaper sessions is warranted"). |
| Graph-caller limitation | Confirmed and correctly disclosed. GitNexus's `context` call for `recall_skills` returned an empty `incoming` set, while Serena's `find_referencing_symbols` for the same symbol found real callers in `context_bundle.py`, `cli_handlers_skills.py`, and `mcp_server.py` (verified in `serena-recall-skills-references.json`). `REPORT.md` labels this "GitNexus completeness FAIL for this symbol" — accurate, not overclaimed. |
| Zero-based line limitation | **Confirmed as a live, reproducible defect** (see Finding 1). |
| AGENTS.md/CLAUDE.md preservation | Confirmed. Current `AGENTS.md`/`CLAUDE.md` are exact byte-identical prefixes of `AGENTS.md.indexer-output`/`CLAUDE.md.indexer-output` (verified programmatically) — the indexer-appended `<!-- gitnexus:... -->` block was cleanly separable and removed without corrupting the hand-maintained contract files. |

## Findings

### Finding 1 — Zero-based line off-by-one is real in all three live clients, and the fix is unverified (Medium)

All three live smoke transcripts report the citation as **lines 680–744** for `recall_skills`:
- `codex-smoke.stdout`: agent message says "lines **680–744**."
- `claude-smoke.stdout`: result says "lines 680–744."
- `agy-smoke.stdout`: response says "lines 680–744."

The actual 1-based editor line is **681** (`grep -n "^def recall_skills" memorymaster/knowledge/skills.py` → `681:def recall_skills(`). Serena's `body_location.start_line: 680` is zero-based per the documented convention (`docs/code-navigation.md:26-27`, and the `serena-navigation.yml` prompt: "body_location lines are zero-based: add one for editor/file citations"). All three clients received that instruction (it's baked into the installed Serena context prompt used for every call) and all three still reported the raw zero-based number unmodified.

`REPORT.md` already discloses this exact gap ("The first CLI results repeated Serena's zero-based range 680–744; the actual editor range is 681–745... Installed navigation instructions now explicitly require adding one... the paid client smokes were not repeated solely for this prompt change.") — so this is not a hidden defect. But it means the fix (an instruction-wording change) has **no live re-verification**: there is currently no evidence the off-by-one is actually corrected in practice, only that the instruction text was edited after the fact. Until a rerun confirms it, agents using this setup should be assumed to still misreport lines by one.

**Recommendation:** re-run at least one of the three client smokes (cheapest: Codex, which already has full JSONL tracing) after the instruction change, and record whether the citation becomes 681–745.

### Finding 2 — AGY's permission-denial "repair" bypasses permissions rather than scoping them; not disclosed as such in REPORT.md (Medium)

`client_smoke.py`'s AGY invocation unconditionally passes `--dangerously-skip-permissions`. The `.first-attempt` receipt for AGY shows a denial (`denied_actions: [{"action":"mcp","display_name":"CallMcpTool"}]`) with this stderr from the tool itself:

> "a tool required the 'mcp' permission that headless mode cannot prompt for, so it was auto-denied. Add an allow-rule under `permissions.allow` in settings.json (e.g. `mcp(<target>)`). Alternatively, re-run with `--dangerously-skip-permissions` to auto-approve all tools."

The repaired run took the second (blanket bypass) option, not the first (scoped allow-rule) that the tool itself suggested. This is methodologically different from Claude's repair, which used a scoped `--allowedTools mcp__serena__find_symbol,mcp__gitnexus__context` plus `--strict-mcp-config` — i.e., Claude's evidence shows the two tools work under a *normal, scoped* permission grant, while AGY's evidence shows they work only with *all permission checking disabled*. `REPORT.md`'s "Repairs" section mentions the AGY conversation was "resumed after initial MCP permission denial" but does not name the bypass mechanism, and its verdict table entry ("PASS for client-reported calls") reads as equivalent in kind to Claude/Codex's rows, which it is not.

**Recommendation:** either re-run AGY with a scoped `permissions.allow` rule (as its own error message suggests) to get comparable evidence quality, or explicitly annotate the AGY row in `REPORT.md`/`docs/code-navigation.md` as "verified only with permission checks disabled," so a reader doesn't infer AGY's default/safe-mode behavior from this receipt.

## Not re-litigated

Per task scope, no provider/tool calls were made and no new tests beyond the two already-existing
suites (`tests/test_code_navigation_preflight.py`, `ruff`) were run. `.gitnexus/meta.json` freshness,
GitNexus/Serena installation correctness on other machines, and the `.planning/audits/2026-09-28-
code-intelligence/REPORT.md` narrative outside the two findings above were read but not independently
re-derived beyond the cross-checks listed.

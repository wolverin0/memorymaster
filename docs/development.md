<!-- doc-head: scoped verification, Windows hook commands, positional trust and native OpenCode loading -->
# Development guidance
Covers: disposable checks, Codex roots and trust, OpenCode plugin loading, runtime evidence and code navigation.
Key terms: source versus installed runtime, risk-based tests, SQLite, agent instructions.
Read when: implementing changes or choosing the smallest sufficient verification.
<!-- /doc-head -->

Install development dependencies with `python -m pip install -e ".[dev,mcp,security]"`.
Use `python -m pytest` from the intended checkout so source-under-test is explicit.
The minimal product depends only on requests and tenacity (the Jev decisions
transport is the standard library's http.client); provider, vector and MCP adapters are optional extras. Upstream research is not a runtime dependency
inventory; `pyproject.toml` is the dependency contract.

## Verification by change

| Change | Appropriate local evidence |
|---|---|
| Documentation / task prompt | Check linked paths, commands and factual assertions against source/runtime |
| Capture / recall / retirement | `python -m pytest tests/test_public_demo.py tests/test_public_v1.py -q` |
| Graph observations | `python -m pytest tests/test_graph_observations.py tests/test_public_demo.py -q` |
| Compiled profile | `python -m pytest tests/test_compiled_profile.py tests/test_compiled_profile_surfaces.py tests/test_profile_config_defaults.py -q` |
| Shared core / schema | Relevant regression and migration/restore tests, then `python -m pytest tests/ -m "not ml" -q --tb=short` |
| Changed ML/vector behavior | Relevant `ml` tests in addition to normal tests |
| Changed Python | `python -m ruff check <changed-files>` |

There is no hosted CI (removed 2026-10-25). `python scripts/bulk_check.py` runs the
fast gates (release truth, contract witness, probes frozen, branch freshness, MCP
stdio smoke) and the full non-ML suite in parallel on this host; `--ml` adds the
isolated ML suite. Exit 0 on the branch is the merge gate; `--fast` is for iteration.
After changing MCP tools, CLI commands, console entrypoints or setup profiles,
regenerate release metadata with `python scripts/generate_release_truth.py`, then
run it with `--check`. Adding tests does not change it: the test-function count is
printed at run time, not committed, so parallel PRs do not conflict on it.

## One observable memory journey

`python -m memorymaster --json demo` uses disposable SQLite, fixture providers,
explicit promotion, cited recall and public source retirement. Its report checks
that retired claims and stale observations disappear from trusted recall.
Ordinary recall must exclude observations even before retirement.
It does not establish provider quality, live MCP/hook delivery, or production
precision; verify those through dated installed-runtime evidence when in scope.

Never use `run-cycle` against the live database as a routine code test.
Use read-only operational review for live inspection. A successful review with
disabled subsystems establishes their configuration, not synthesis quality.
Distinguish requested, configured and actually observed provider models.

## Codex deterministic recall (T-1800)

Install into an explicitly selected Codex config directory; the callable has no
implicit home target. Run from the checkout, replacing the target argument:

```powershell
python -c "from pathlib import Path; from memorymaster.surfaces.setup_hooks import install_codex_recall_hooks; install_codex_recall_hooks(codex_dir=Path(r'<codex-config-dir>'), project_root=Path.cwd())"
```

For a code-only worktree, also pass `data_root=Path(r'<memory-data-dir>')`,
the checkout holding `memorymaster.db` and the wiki. SessionStart renders
`MM_ROOT` from `data_root`; the recall script and Codex adapter render
`PROJECT_ROOT` from `project_root`. Omitting `data_root` keeps the original
single-root behavior. Both paths use the Claude installer's placeholder helper.

This merges SessionStart and UserPromptSubmit beside the existing Stop capture
hook. The thin adapter runs the same rendered Claude hook templates, preserving
their retrieval, filtering, scope, projection and delivery policy. Codex output
adds a conservative 1,800 UTF-8 byte ceiling (including non-Latin text); empty
results and errors produce no stdout/stderr and exit zero. Internal Codex
extractors/workers must inherit `MEMORYMASTER_INTERNAL_CODEX=1` or
`MEMORYMASTER_INTERNAL_RUN=1`; both events then skip before reading stdin.

On Windows the recall installer writes the same `command` and `commandWindows`.
Paths use forward slashes. When neither path contains whitespace or Windows
shell metacharacters, the command is
`<python.exe> <memorymaster-codex-recall.py> --event <event>` with no quotes.
For paths that need shell quoting it uses
`cmd.exe /d /v:off /s /c ""<python.exe>" "<memorymaster-codex-recall.py>" --event <event>"`.
The leading executable remains unquoted and the nested shell preserves the
quoted paths and stdin. Paths containing a literal percent sign are rejected
because `cmd.exe` expands `%NAME%` even inside quotes. This follows the Windows `cmd /C` execution in
[Codex 0.164.0's command runner](https://github.com/openai/codex/blob/rust-v0.164.0/codex-rs/hooks/src/engine/command_runner.rs).
A command starting directly with a quoted Python executable failed to launch
in the operator's real CLI test (T-1806). The [hook reference](https://learn.chatgpt.com/docs/hooks)
documents `commandWindows` as the Windows-only override.

Successful injections append `{ts, cli, event, count}` JSONL through
`append_hook_event('codex', event, count)`. `count` measures claim citation
bullets in the delivered text, after capping. A profile/activity-only injection
has count zero. Set `MEMORYMASTER_HOOK_EVENTS_LOG` to choose the log; the default
is `.memorymaster/hook-events.jsonl` under the user's home.

[Official OpenAI hook documentation](https://learn.chatgpt.com/docs/hooks)
requires reviewing new/changed non-managed hooks through `/hooks`, trusting the
current definition hash. Installation does not grant persistent trust. For a
lane that has vetted every enabled hook, the documented non-interactive route
is `--dangerously-bypass-hook-trust`, effective only for that invocation. It
does not disable the command sandbox or grant persistent hook trust.

A persistent session without that bypass needs each installed hook trusted once
through `/hooks`. Codex stores `trusted_hash` in `config.toml` under
`[hooks.state.'<hooks.json path>:<event>:<group>:<hook>']`; events in these keys
use snake case, for example `session_start` and `user_prompt_submit`. Group and
hook positions are zero based. Moving a handler or changing its definition can
require another review. The installer preserves peer handlers but never writes
trust state or synthesizes hashes. The official hook and CLI references and
installed `codex exec --help` expose no supported non-interactive command for
granting persistent trust; the supported automation flag only bypasses review
for that invocation. The state-key construction is explicit in
[Codex's hook_key](https://github.com/openai/codex/blob/rust-v0.164.0/codex-rs/hooks/src/lib.rs); the trust hash is persisted by the
[interactive hook review](https://github.com/openai/codex/blob/rust-v0.164.0/codex-rs/tui/src/hooks_rpc.rs).

After the lane installs into its real CLI config, run this exact command from
the checkout (with the internal-run markers absent):

```powershell
$env:MEMORYMASTER_HOOK_EVENTS_LOG = Join-Path (Get-Location) 'codex-hook-events.jsonl'
codex --no-daemon exec --dangerously-bypass-hook-trust --sandbox read-only --ephemeral --json --cd . -m gpt-6.1-sol -c model_reasoning_effort=high "Explain MemoryMaster SQLite authority, scope isolation and citations using any automatically injected memory. Include its memory citations. Do not call tools."
Get-Content $env:MEMORYMASTER_HOOK_EVENTS_LOG
```

Require fresh `codex` SessionStart/UserPromptSubmit injection records and inspect
the response for supplied citations; an empty result correctly emits no record.
This lane tested source and disposable installation only. Real-config install,
real CLI injection, the bulk merge gate and PR remain the integrating lane's work.
The installed CLI help was checked at version 0.163.0 and exposes the trust flag.

Local regression evidence: the adapter and telemetry tests first failed because
the implementations were missing; a mixed-entry preservation regression then
failed before the installer fix. The first ordinary pytest runs failed during
temporary-directory setup: Windows sandbox ACLs made Python's mode-0700
directories inaccessible. Successful narrow runs used a process-local test
launcher mapping mkdir mode 0700 to inherited workspace permissions (0777),
with `--basetemp=.tmp/<run>`; product code and sandbox policy were unchanged.
The five single-file runs passed 65 cases in total: Codex adapter (20), installer
(3), shared telemetry (2), setup hooks (38) and public demo (2). Ruff passed on
all changed Python files; release-truth generation/check and contract witness
against origin/main passed. A separate read-only review found no remaining
blockers after the peer-handler fix. The bounded installer worker used
gpt-5.6-luna/medium; the reviewer used gpt-5.6-sol/high.

The 2026-10-26 follow-up reproduced count zero for hex citations (including
`~2` suffixes) and rejection of a separate `data_root` before fixing them.
Fresh disposable checks passed all 64 cases across the four hook/installer
test files, plus both public-demo cases, Ruff, release-truth verification and
contract witness. SessionStart also injected from a fixture database under a
separate data root. No real home installation or live database was used.

## OpenCode Windows repair (T-1806, 2026-10-26)

The installer and `--opencode-config-dir` now default to the native v2 plugin.
The installed npm `@opencode/cli` 2.1.1 binary's `PluginModule.load` validates
`module.default` as `{id, setup}` or `{id, effect}`; it calls the `setup` adapter,
not a `server` callback. This agrees with the
[native plugin contract](https://opencode.ai/v2/docs/build/plugins).
The shared adapter registers `ctx.session.hook("prompt", ...)` to remember the
latest user input and `ctx.session.hook("context", ...)` to append recalled text
to the model's structured `system` array. The existing v2 terminal-event capture
and cleanup behavior is preserved.

Use `--opencode-version 1` only for the legacy API. Its named function export
stays intact: the old loader invokes every exported value as a function, so
adding a native default object there would break legacy autoload. Regression
tests exercise both the native default installation and legacy export enumeration.

For a fresh CLI test, isolate `OPENCODE_CONFIG_DIR`, `XDG_CONFIG_HOME`,
`XDG_DATA_HOME`, `XDG_STATE_HOME`, `XDG_CACHE_HOME` and the temporary directory.
Use `opencode run --standalone -m opencode-go/deepseek-v4.1-flash`, or start an
isolated temporary server and pass its URL with `--server`. A plain `run` can
reuse the background service. Align `PWD` with the subprocess working directory:
2.1.1's run command prefers inherited `PWD` over `process.cwd()`.

Fresh temporary-profile evidence in this sandbox confirmed native `setup`,
`prompt` and `context` callbacks. The Python recall subprocess then failed with
`EPERM` (`uv_spawn`), with zero recalled bytes and no injection receipt. A
separate real model run ended with `provider.transport / ConnectionRefused`.
Running from the checkout also hit `EPERM` spawning Git during project discovery.
Thus no real CLI answer containing the requested value and citation was observed;
real delivery acceptance remains open. Capture was explicitly disabled and only
the existing OpenCode Go credential was copied into disposable CLI state; no real
CLI configuration or authoritative memory database was modified.

The first focused test run failed on mode-0700 Windows fixture ACLs. A later
native-default regression failed before switching the installer default to v2.
Fresh checks used the process-local mode-0777 fixture launcher described above.
Codex's native command-shell tests verified stdin and spaced/metacharacter paths;
the real temporary-home Codex probe stalled before its hook marker. Persistent
hook trust and real-config delivery still require the integrating lane's check.
The integrated touched-test/public-demo run passed 103 cases (one existing
Pydantic deprecation warning), and Ruff passed all seven changed Python files.
Release truth first reported stale test counts; regenerating its inventories
made `--check` pass. Independent read-only review found no remaining code
blockers after the legacy-loader and Windows metacharacter fixes. These source
checks do not establish real CLI delivery.
Sanitized diagnostic evidence was retained outside the checkout. Recursive
scratch deletion was rejected by the tool policy, and moving the remaining
`.tmp` fixtures failed on Windows ACLs; the integrating lane must remove those
leftovers outside this sandbox. Temporary Codex probe termination could not be
confirmed because process command-line inspection was denied; no process with
unproven ownership was stopped.

## Optional wiki

The wiki is an opt-in projection (`MEMORYMASTER_WIKI_ABSORB=1`), not recall
authority. Keep frontmatter title, 50-200 character description, type, scope,
tags and date, and a wikilink for bodies over 300 characters. Generated
`obsidian-vault/bases/*.base` files are rebuilt by wiki absorb, not hand-edited.

## Code intelligence

Use [the shared navigation workflow](code-navigation.md) for AGY, Claude Code and
Codex. Run `python scripts/check_code_navigation.py` before trusting this
checkout's index. Dirty/new code still requires live-source verification even
when indexed HEAD matches. Serena supplies bounded live symbol/reference reads;
an empty GitNexus incoming set is not proof that no callers exist.

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

Select the index for the checkout being changed; consult its metadata for freshness.

> If any GitNexus tool warns the index is stale, run `npx gitnexus analyze` in terminal first.

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `gitnexus_impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `gitnexus_detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `gitnexus_query({query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `gitnexus_context({name: "symbolName"})`.

## When Debugging

1. `gitnexus_query({query: "<error or symptom>"})` — find execution flows related to the issue
2. `gitnexus_context({name: "<suspect function>"})` — see all callers, callees, and process participation
3. `READ gitnexus://repo/memorymaster/process/{processName}` — trace the full execution flow step by step
4. For regressions: `gitnexus_detect_changes({scope: "compare", base_ref: "main"})` — see what your branch changed

## When Refactoring

- **Renaming**: MUST use `gitnexus_rename({symbol_name: "old", new_name: "new", dry_run: true})` first. Review the preview — graph edits are safe, text_search edits need manual review. Then run with `dry_run: false`.
- **Extracting/Splitting**: MUST run `gitnexus_context({name: "target"})` to see all incoming/outgoing refs, then `gitnexus_impact({target: "target", direction: "upstream"})` to find all external callers before moving code.
- After any refactor: run `gitnexus_detect_changes({scope: "all"})` to verify only expected files changed.

## Never Do

- NEVER edit a function, class, or method without first running `gitnexus_impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `gitnexus_rename` which understands the call graph.
- NEVER commit changes without running `gitnexus_detect_changes()` to check affected scope.

## Tools Quick Reference

| Tool | When to use | Command |
|------|-------------|---------|
| `query` | Find code by concept | `gitnexus_query({query: "auth validation"})` |
| `context` | 360-degree view of one symbol | `gitnexus_context({name: "validateUser"})` |
| `impact` | Blast radius before editing | `gitnexus_impact({target: "X", direction: "upstream"})` |
| `detect_changes` | Pre-commit scope check | `gitnexus_detect_changes({scope: "staged"})` |
| `rename` | Safe multi-file rename | `gitnexus_rename({symbol_name: "old", new_name: "new", dry_run: true})` |
| `cypher` | Custom graph queries | `gitnexus_cypher({query: "MATCH ..."})` |

## Impact Risk Levels

| Depth | Meaning | Action |
|-------|---------|--------|
| d=1 | WILL BREAK — direct callers/importers | MUST update these |
| d=2 | LIKELY AFFECTED — indirect deps | Should test |
| d=3 | MAY NEED TESTING — transitive | Test if critical path |

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/memorymaster/context` | Codebase overview, check index freshness |
| `gitnexus://repo/memorymaster/clusters` | All functional areas |
| `gitnexus://repo/memorymaster/processes` | All execution flows |
| `gitnexus://repo/memorymaster/process/{name}` | Step-by-step execution trace |

## Self-Check Before Finishing

Before completing any code modification task, verify:
1. `gitnexus_impact` was run for all modified symbols
2. No HIGH/CRITICAL risk warnings were ignored
3. `gitnexus_detect_changes()` confirms changes match expected scope
4. All d=1 (WILL BREAK) dependents were updated

## Keeping the Index Fresh

After committing code changes, the GitNexus index becomes stale. Re-run analyze to update it:

```bash
npx gitnexus analyze
```

If the index previously included embeddings, preserve them by adding `--embeddings`:

```bash
npx gitnexus analyze --embeddings
```

To check whether embeddings exist, inspect `.gitnexus/meta.json` — the `stats.embeddings` field shows the count (0 means no embeddings). **Running analyze without `--embeddings` will delete any previously generated embeddings.**

> Claude Code users: A PostToolUse hook DETECTS staleness after `git commit`/`git merge` and reminds you to re-run analyze — it does NOT reindex automatically (by design, to avoid blocking your commit). Run `npx gitnexus analyze --embeddings` yourself when the reminder fires.

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->

<!-- doc-head: shared CLI navigation with bounded live symbol retrieval -->
<!-- Covers: GitNexus, Serena, native search, index freshness and verification limits. -->
<!-- Read when: locating code or configuring AGY, Claude Code and Codex. -->
<!-- /doc-head -->
# Code navigation

MemoryMaster owns governed memory. Code indexes are disposable navigation aids,
not claims, citations, authorization or a second durable memory system.

## Shared workflow

1. Run `python scripts/check_code_navigation.py` from this checkout. A matching
   commit does not prove coverage of dirty or new files. `WARN` requires live
   verification; never use another worktree's index implicitly.
2. For a known path or identifier, start with scoped `rg`/native search.
3. For behavior or dependency questions, use GitNexus `query`, then `context`
   or `impact`. Specify the current repository, use a small result limit and
   omit source bodies initially. A missing edge does not prove no callers.
4. Use Serena `get_symbols_overview` or `find_symbol` to retrieve live code.
   Request only the required body with `max_answer_chars=6000`. On overflow,
   narrow the symbol or read a bounded source range instead of repeatedly
   increasing the limit. Check `find_referencing_symbols` before risky edits.
5. Verify the live source and run affected tests. Neither graph completeness
   nor successful tool invocation establishes correctness of a change.

If the client defers MCP tools, use its tool discovery before declaring a
configured server unavailable. One Codex follow-up falsely reported Serena
unavailable; explicit discovery in a subsequent fresh session exercised both
tools successfully. Keep the failed attempt as a client-routing limitation.

Serena's `body_location` lines are zero-based. Add one when reporting editor
line numbers. Method bodies start at the symbol's first-line column; this is
not a whole-body dedent. Windows paths may use either separator.

## Installed configuration

The September 28 setup uses GitNexus 1.4.7 and Serena 1.7.0 in all three
clients. Native Codex and Orca's managed Codex home are both configured.
Serena starts as a separate stdio subprocess per client, with
`--project-from-cwd`; no shared mutable project-selection server is used.
The checked-in template is
`memorymaster/config_templates/code-intelligence/serena-navigation.yml`.
Its installed copy is `~/.serena/contexts/code-navigation.yml`.

Only five Serena tools are exposed: project activation/status, symbol overview,
symbol lookup and symbol references. `no-memories` and `no-onboarding` modes
avoid introducing a competing memory workflow. Dashboard/browser startup is off.
Editing remains with the client's existing tools. Existing sessions may require
a restart to load newly added MCP servers; fresh clients were tested.
Claude and AGY have exact allow-rules for these five Serena tools and GitNexus
`query`, `context`, `impact`, `list_repos`, `detect_changes`. No blanket MCP,
shell or editing permission was added. AGY's final permission validation ran
without the one-off bypass used by an earlier, retained diagnostic attempt.

Short navigation instructions are appended to the clients' global instruction
files. Existing contents are preserved. Private configuration backups and exact
target manifests are in `~/.local/state/code-intelligence-20260928/`.
Rollback must remove only this task's additions and preserve later user edits;
do not blindly replace a concurrently changed global configuration.

## Index maintenance and optional tools

`.gitnexusignore` excludes upstream research clones and generated artifacts.
Refresh this project's index with `gitnexus analyze --embeddings` to preserve
embeddings. This installed indexer appends generated instruction blocks:
review its diff and preserve the hand-maintained AGENTS/CLAUDE contracts.
Do not install a second post-commit hook over the operator's existing hooks.

Graphify remains available on demand. The installed package is `graphifyy`
0.4.0, whose package metadata points to `safishamsi/graphify`; it must not be
silently treated as a verified installation of a different upstream release.
The existing graph report is dated July 23 and is not a current-source oracle.
The bounded skill-catalog query returned unrelated files from cloned upstream
repositories, confirming that this graph is unsuitable for default navigation.
Use bounded queries, never load the whole graph/report into model context.
GraphRAG inside MemoryMaster is a separate governed-recall feature; its
activation was not changed by this work.

GitNexus's installed package declares `PolyForm-Noncommercial-1.0.0`.
This setup does not establish permission for every commercial deployment.

## Evidence and limits

The frozen 20-task experiment and client smoke results are in
`.planning/audits/2026-09-28-code-intelligence/REPORT.md`.
Tool payload reduction is different from total session token/cost savings.
Keep initialization, tool schemas, cached input, repeated turns and indexing
costs visible when evaluating future changes. Do not infer savings by comparing
a bounded lookup with a deliberately unnecessary read of the whole repository.

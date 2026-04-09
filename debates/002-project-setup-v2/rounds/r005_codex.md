# Codex - R5: Final Verdict

Ship `/project-setup` only after these exact edits to `project-setup/SKILL.md`:

1. Replace shallow shell discovery with recursive glob discovery. Search from repo root for manifests and entrypoints (`**/package.json`, `**/pyproject.toml`, `**/go.mod`, etc.) instead of assuming a flat layout.
2. Ban destructive verification by default. Verification must be a read-only allowlist only: `--help`, `--version`, `--co`, `--list*`, file existence checks, and import checks. Explicitly forbid tests, builds, migrations, seeders, and networked/e2e commands unless the user opts in.
3. Add idempotency markers to generated files. Persist at least a source-manifest hash and chosen scope marker. If markers still match, rerun must no-op or refresh only safe sections unless `--force` is provided.
4. Lock scope on first successful run. Later runs must read the existing scope marker from generated files and never re-derive scope from cwd or remotes.
5. Make writes atomic. Write temp files, then replace in one step; if markers changed during generation, abort instead of clobbering concurrent edits.

**Confidence: 0.97.** These changes remove the real failure modes: unsafe execution, rerun drift, scope drift, partial writes, and monorepo miss-detection.

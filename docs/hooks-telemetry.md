<!-- doc-head: per-CLI hook telemetry and disposable contract diagnosis -->
<!-- Covers: log schema, MCP attribution, doctor limits and operational alarms. -->
<!-- Read when: checking whether a CLI automatically received memory. -->
<!-- /doc-head -->
# Hook telemetry and diagnosis

Hooks append content-free delivery receipts with
`append_hook_event(cli, event, count)`. `MEMORYMASTER_HOOK_EVENTS_LOG` overrides
the default `MEMORYMASTER_HOME/hook-events.jsonl`; the default home is
`~/.memorymaster`. Records contain only `ts` (UTC), `cli`, `event`, and integer
`count`. A successful context emission logs `recall_injected` with count one.
Suppressed, empty and failed deliveries do not count. Rotation keeps five
5 MiB segments plus the current file, with a shared process lock.

MCP usage attributes sessions to initialize `clientInfo.name`, independently
of authorization principals. `agent_sessions` persists the client and served
recall/ingest counters. `get_usage_rollup` reads those counters after restart;
other process metrics remain process-local. Existing unattributed history is
not reconstructed, and empty recalls retain their historical zero-count rule.

Run fixture diagnostics against explicit directories:

```powershell
memorymaster --json hooks doctor --claude-dir <claude-config> --codex-dir <codex-config> --agy-dir <agy-config> --opencode-dir <opencode-config>
```

The agy target is the directory containing its named-group `hooks.json`,
typically the CLI's `config` directory. Codex JSON and inline TOML hooks are
inspected (inline TOML requires Python 3.11+). Exit codes are 0 PASS, 1 FAIL,
3 WARN; no directories means a usage error. Missing hooks and capture-only
installations cannot pass as automatic memory delivery.

Each supported Python hook command runs in a fresh child with synthetic
CLI-specific stdin, an empty transcript and one disposable confirmed memory.
The installed script's bootstrap root is redirected to the fixture; recall
still uses the shared core. Provider credentials and live activation settings
are excluded. Audit guards block external data reads/writes, SQLite databases,
network connections and child workers. Parent repository lookups see absent
repositories. This is a cooperative contract harness, not an untrusted-code
sandbox or an installed-runtime acceptance test.

The doctor accepts Claude additionalContext, Codex additionalContext and its
supported plain-stdout events, and agy string injectSteps. It rejects quoted
agy commands because that CLI does not remove command quotes. Nested agy
systemMessage schemas, shell commands and OpenCode plugin execution require
their runtime-specific probes; they are never certified as PASS here.
OpenCode plugin discovery reports a WARN with the installed plugin filenames.

`install_hooks(..., config_dir=...)` and
`install_dream_hooks(..., claude_dir=..., codex_dir=..., hook_dir=...)` support
explicit fixture targets. An explicit dream config target also becomes the
script location if `hook_dir` is omitted. Reinstallation preserves peer entries
and does not duplicate managed hooks.

Operational review reads current and rotated receipts for 24-hour counts per
CLI. Captured CLIs with zero deliveries WARN, as do missing/corrupt telemetry.
The seven-day capture yield compares distinct captured sessions against new
`add` application claims, verifying creation times in authoritative SQLite.
Reinforcement and duplicate claim IDs do not increase yield. A ratio below
0.05 FAILs. Set `MEMORYMASTER_CAPTURE_MIN_MEMORIES_PER_SESSION` explicitly to
override this threshold; invalid values FAIL. Claims have second-precision
timestamps, so capture lower bounds use the same precision.

For the lane's real Codex acceptance after installation, start `codex`, inspect
and trust the installed hooks using `/hooks`, then run:

```powershell
codex exec --json "Describe the MemoryMaster storage authority and cite the injected memory."
```

Check new `cli=codex`, `event=recall_injected` receipts in the configured log
and the session's injected context/citations. Hook trust and real CLI delivery
remain separate from fixture PASS. This telemetry card does not install new
Codex, agy or OpenCode recall adapters; their integration cards own those.

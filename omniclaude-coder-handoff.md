# OmniClaude — Handoff to Implementer Session

You are the implementer session for **OmniClaude**, the single-inbox
orchestrator that pauol is building to end the "20 dashboards problem". This
document is your full context. Read it end-to-end before writing code. The
design was refined across 3 sessions (planning, critique, re-planning) — do
not re-litigate it, build it.

---

## 1. The Problem (real, not abstract)

pauol currently has monitoring signals and agents scattered across:

- **WispBot** (MikroTik WAN monitoring, 24/7)
- **Dashboard bot** (Telegram alerts for payments, customers, tickets)
- **Paperclip** (board-based task tracker with scheduled agents)
- **OpenClaw** (session watcher, session-watcher.cjs, Telegram channel)
- **14+ individual Claude Code sessions** for active development
- **Manual cron jobs** for backups, syncs, scrapers

Each speaks to Telegram through a **different bot**. When something breaks at
3am, the alert lands but nothing happens until pauol sits at the PC. There is
no single place to go, no single channel that knows everything, and no way
for sessions to hand work off to each other.

**Goal**: one channel that receives everything, knows what each project is,
optionally acts on its own, and pings pauol when it cannot.

---

## 2. The Solution (architecturally)

OmniClaude is the **single reader** of a centralized inbox, not the single
executor. Everything else keeps running where it runs today.

```
Paperclip      →                      ←  Claude sessions
OpenClaw       →                      ←  daily-digest cron (9am)
WispBot        → ~/.omniclaude/       ←  WAN alerts
Dashboard bot  →    inbox.jsonl       ←  Dashboard events
14 projects    →                      ←  (file watchers, cron scripts)
                     ↓ single read
                   OmniClaude
                     ↓ single write
                 ~/.omniclaude/
                  outbox.jsonl
                     ↓
          Telegram (one bot, one chat)
                   +
           Spawn Claude sessions
                (via wezbridge)
```

### Key principle: centralize the INBOX, not the execution

Paperclip, OpenClaw, WispBot, and every script keep doing exactly what they
do today. They just each write one JSONL line per event to
`~/.omniclaude/inbox.jsonl`. OmniClaude is the reader.

---

## 3. The Five Pieces (in build order)

### Piece 1 — `~/.omniclaude/inbox.jsonl` (schema, no code)

Append-only JSONL file. Every event is one line. Schema:

```json
{
  "ts": "2026-04-10T14:02:00-03:00",
  "source": "wan-alerts | paperclip | claude-session | openclaw | wispbot | dashboard | custom",
  "project": "<project name, matches monitoring.md frontmatter>",
  "severity": "P0 | P1 | P2 | P3 | info",
  "event": "<snake_case event type>",
  "details": "<one-line human summary>",
  "payload": { "...optional structured data..." },
  "needs_action": true | false,
  "incident_id": "<optional, for event correlation across sources>"
}
```

**Writers** just append. **Readers** tail. No locking, no database, no
schema migrations. `~/.omniclaude/` is the base dir; create on first write.

### Piece 2 — `monitoring.md` per project (skill already exists)

The skill `monitoring-setup` at `~/.claude/skills/monitoring-setup/SKILL.md`
is already written. It generates `monitoring.md` in each project root with:

- **Frontmatter**: project metadata (name, path, stack, entry_point,
  test_command, health_check, mcp_servers, telegram_escalation, omniclaude
  operational config)
- **What This Project Does** (2-3 sentences)
- **Architecture Summary** (from graphify GRAPH_REPORT.md)
- **Current State** (last 5-10 commits, what's broken, recent claims)
- **Key Files** (top 5-10, usually god nodes from graphify)
- **Active Issues** (bugs, blockers, MemoryMaster claims of type bug/gotcha)
- **Monitoring Signals** (each signal has: severity, type, source,
  monitor_script with pre-filter pipeline, trigger_pattern, action_level,
  action_script, cooldown_minutes, max_actions_per_hour, depends_on,
  escalate_to)
- **Events This Project Emits** (what this project writes to inbox.jsonl)
- **How to Verify It Works**
- **Dependencies on Other Projects**

**Your job is NOT to rewrite the skill.** Treat `monitoring.md` as input —
when OmniClaude sees a project name in an inbox event, it reads that
project's `monitoring.md` for context.

**Graphify is MANDATORY per project** (never optional). If a project has no
`graphify-out/`, the monitoring-setup skill refuses to generate monitoring.md
until graphify runs. Same rule for OmniClaude: if a project appears in
inbox.jsonl but has no monitoring.md, OmniClaude escalates "no context for
this project" to Telegram and refuses to act.

### Piece 3 — `scripts/daily-digest.cjs` (build this first)

A standalone Node script, runs from Windows Task Scheduler at 09:00 ART.
Does NOT require OmniClaude to be running. Does NOT spawn anything.

**Responsibilities**:

1. Read `~/.omniclaude/projects-watched.json` (array of project paths,
   pauol curates by hand initially)
2. For each project:
   - `git log -5 --oneline` (last commits)
   - `git status -s | wc -l` (uncommitted change count)
   - Read `monitoring.md` frontmatter → get `test_command`, `health_check`
   - Run `test_command` if not "none", capture exit code (skip if takes >30s)
   - Run `health_check` if not "none"
   - Query MemoryMaster: `memorymaster query-memory "<project>"` scoped to
     `project:<name>`, filter claims created since yesterday where
     `claim_type in (bug, gotcha, constraint)`
3. Tail `~/.omniclaude/inbox.jsonl` for events in the last 24h, group by project
4. Build a markdown digest:
   ```
   ☀️ Digest 2026-04-10 09:00

   🔴 gimnasio — 3 tests failing since yesterday (last commit 18h ago)
      └ inbox: 2 P1 events (build_broken, ci_fail)
   🔴 wispbot — link CABA-Córdoba down 20h (no-one handled)
      └ inbox: 4 P0 events since 14:00 yesterday
   🟡 impulsa — session pidió decisión ayer 18:00 (sin respuesta)
      └ inbox: 1 info event (claude_needs_decision)
   🟢 11 otros OK

   Responde "fix gimnasio" / "ver wispbot" para spawn sesión.
   ```
5. Send to Telegram via bot API (direct curl/fetch to
   `https://api.telegram.org/bot<TOKEN>/sendMessage`, no channel framework)
6. Append the digest to `~/.omniclaude/digests/YYYY-MM-DD.md` for history
7. Write one summary event to `inbox.jsonl`: `{source: "daily-digest",
   event: "digest_sent", details: "X projects red, Y yellow, Z green"}`

**This is Milestone 0. Ship this first. Nothing else until this works end-to-end.**

### Piece 4 — OmniClaude live session (Milestone 1)

A permanent Claude Code session running with `--channels telegram`, Monitor
tool actively tailing `~/.omniclaude/inbox.jsonl`. Reacts to events with
severity P0/P1 in real time without waiting for the 9am digest.

**IMPORTANT constraint** (claim #8480 in MemoryMaster, read it): Monitor tool
is session-scoped. If the OmniClaude Claude Code session crashes (OOM, rate
limit, bug), all Monitors die. So you need an **external watchdog** (systemd
timer on Linux, Task Scheduler on Windows, OpenClaw task) that verifies the
OmniClaude session is alive every 5 minutes and relaunches if dead. The
watchdog must also re-subscribe the Monitor on relaunch.

**Behavior**:
- On P0/P1 event: read the project's `monitoring.md`, correlate with recent
  inbox events (same incident_id or same project in last N minutes), write a
  brief Telegram alert with context, wait for pauol to react
- On P2/P3: log to digest only, don't interrupt
- On claude_needs_decision events (from other sessions): forward the question
  to Telegram with context
- Never spawn anything in M1. That's M2.

### Piece 5 — Spawn-on-reply (Milestone 2)

pauol replies to a Telegram alert with "fix gimnasio" or "retry wispbot".
OmniClaude parses the reply, reads the project's `monitoring.md`, spawns a
Claude Code session via `mcp__wezbridge__spawn_session` with a rich context
prompt:

```
A signal fired at <ts> in project <name>:
- signal: <signal_id from monitoring.md>
- pattern matched: <trigger_pattern>
- last 50 lines of context: <from monitor_script output>
- inbox events in last 1h: <correlated events>
- project architecture summary: <from graphify GRAPH_REPORT.md>
- known fixes from memory: <MemoryMaster query for similar past incidents>
- action_level allowed: <from monitoring.md>

Your job: diagnose, attempt fix, re-run <verify_command>, report back via
inbox.jsonl when done.
```

**Constraints** (claims #8444, #8480 in MemoryMaster, read them):

- **Spawn budget global**: max 5 concurrent spawned sessions across all
  projects. Enforced in OmniClaude before spawn.
- **Per-project cap**: max_concurrent_monitors from monitoring.md frontmatter
- **Incident dedup**: multiple signals → 1 incident_id → 1 spawned session at
  a time per project
- **MemoryMaster lookup BEFORE spawn**: query for similar past fixes, pass
  context to the spawned session
- **Act → verify → declare fixed**: the spawned session must re-run the
  trigger signal before reporting success
- **Escalation on failure**: if spawn fails, session times out, or verify
  fails after max retries, escalate to Telegram with full context
- **cooldown_minutes + max_actions_per_hour**: circuit breaker, escalate
  instead of retrying forever
- **active_hours**: outside the window, downgrade all actions to read_only
- **pre-filter at bash level**: every monitor_script pipeline must be
  `tail | grep pattern | awk dedupe | sed redact | awk rate_limit` BEFORE
  stdout reaches Claude
- **Secrets redaction**: scripts MUST sed-redact credentials before stdout
- **Network partition detection**: before marking services down, verify
  internet with `ping 8.8.8.8` — if network is down, escalate once and
  suppress further alerts until restored

### Milestone 3 (later, do not build now)

Full autonomous spawn based on pattern matching: known fix in memory →
spawn automatically without reply. Only for fully idempotent actions
(clear cache, restart worker). Never for business logic changes.

---

## 4. Non-Negotiable Constraints

These come from MemoryMaster claims recorded during design. Every one has a
reason, do not skip them:

| # | Constraint | Claim |
|---|---|---|
| 1 | Inbox centralized, execution distributed | Each system keeps doing its job, only the inbox is shared |
| 2 | Monitor tool is session-scoped | Cannot be persistence layer, always need external watchdog |
| 3 | Graphify mandatory per project | Every downstream step assumes GRAPH_REPORT.md exists |
| 4 | Pre-filter at bash level | Claude is never the first filter — regex is |
| 5 | Private IPs NOT redacted at ingest | Infrastructure claims reference them legitimately |
| 6 | Secrets redacted BEFORE stdout reaches Claude | Not after |
| 7 | action_level is a security boundary | Default read_only, only escalate with explicit approval |
| 8 | Circuit breakers mandatory | cooldown_minutes + max_actions_per_hour per signal |
| 9 | Incident dedup cross-signal | Multiple signals → 1 incident → 1 session |
| 10 | Spawn budget global | max 5 concurrent spawned sessions total |
| 11 | Act → verify → declare fixed | No "fixed" without re-running the trigger signal |
| 12 | Dependencies prevent spurious alerts | depends_on DAG in monitoring.md |
| 13 | Active hours gate destructive actions | 3am auto-restart worse than 3am alert |
| 14 | State must survive restarts | state_checkpoint file per project |
| 15 | Network partition detection | ping 8.8.8.8 before marking services down |
| 16 | MemoryMaster lookup before spawn | Don't start from zero when past fix exists |
| 17 | Full autonomy instruction covers the pipeline | "hace TODO" = no pause at high-blast-radius |

Every one of these is already stored as a claim in MemoryMaster. Query them
with `mcp__memorymaster__query_memory` before making architectural decisions.

---

## 5. What Already Exists (do not rebuild)

| Component | Path | Status |
|---|---|---|
| MemoryMaster | `G:\_OneDrive\OneDrive\Desktop\Py Apps\memorymaster\` | v3.3.0 on PyPI, 22 MCP tools, entity registry, typed relationships |
| monitoring-setup skill | `~/.claude/skills/monitoring-setup/SKILL.md` | v3 with mandatory graphify |
| graphify | `pip install graphifyy` | Already installed |
| GitNexus | MCP available | Already integrated |
| session-watcher.cjs | (OpenClaw) | Already emits events |
| wezbridge | MCP available | Has `spawn_session`, `send_prompt`, `wait_for_idle` |
| Telegram bot | OpenClaw pairing active | chat_id: 2128295779 |

---

## 6. Projects to Watch (initial list — pauol will curate)

Located under `G:\_OneDrive\OneDrive\Desktop\Py Apps\`:

- `memorymaster` (Python, PyPI package, priority)
- `whatsappbot-final` (multi-service, production, high priority)
- `wispbot` (WAN monitoring, 24/7, high priority)
- `dashboard` (portal, customer facing, high priority)
- `paperclip` (task control plane, continuous)
- `openclaw` (session watcher, continuous)
- `impulsa` (demo platform)
- `gimnasio-next`, `final-inpla`, `paperclip`, `punto-futura`, `saleor-*`
  (demos / tools)

Each needs `/monitoring-setup` run once before OmniClaude watches it.
Pauol can start with 3-5, not all 14, for the M0 digest.

---

## 7. Stack & Tools You Have Available

- **Language**: Node.js for daily-digest (cross-platform, works on Windows),
  Python for MemoryMaster integration (via CLI or MCP)
- **Task Scheduler**: Windows Task Scheduler for 9am cron (XML schema, write
  the task file alongside the script)
- **Telegram**: direct bot API (`https://api.telegram.org/bot<TOKEN>/sendMessage`),
  no channel framework needed for M0
- **Claude Code MCP tools**: memorymaster, gitnexus, wezbridge (spawn_session),
  Monitor tool (M1+ only)
- **Shell**: bash via Git Bash on Windows, PowerShell for Task Scheduler XML

---

## 8. Your First Task (M0, concrete)

Build `scripts/daily-digest.cjs` in the memorymaster repo. Why there: because
memorymaster already has the Telegram bot credentials, the MemoryMaster CLI
installed, and a git-backed release flow. You can extract it to its own repo
later.

**Steps**:

1. Create `~/.omniclaude/` directory + `projects-watched.json` (start with
   3 projects: memorymaster, whatsappbot-final, wispbot)
2. Create `~/.omniclaude/inbox.jsonl` (empty file, append-only)
3. Write `scripts/daily-digest.cjs` following the spec in Piece 3 above
4. Test it locally: `node scripts/daily-digest.cjs` → should print the
   digest markdown to stdout and NOT send to Telegram (dry-run flag)
5. Once the output looks good, add the Telegram send step
6. Write `scripts/daily-digest.xml` (Task Scheduler task definition) for
   Windows, scheduled 09:00 ART daily
7. Register the task: `schtasks /create /xml scripts\daily-digest.xml /tn
   "OmniClaude Daily Digest"`
8. Trigger it manually once to verify end-to-end
9. Commit as `feat: M0 — daily digest cron for OmniClaude`
10. Write a doc at `docs/OMNICLAUDE_M0.md` explaining what M0 does, what's
    next (M1), and how to add projects to projects-watched.json
11. Ingest a claim in MemoryMaster: subject="OmniClaude M0",
    predicate="daily digest shipped", claim_type="architecture",
    source_agent="omniclaude-coder-session"

**Do not build M1, M2, M3 in this session.** Ship M0, validate it runs for
3 days with pauol watching the digests, then come back for M1.

---

## 9. Output Format Expected

At start:
- Read this document fully
- Query MemoryMaster for the 17 constraint claims (search "OmniClaude")
- Read `~/.claude/skills/monitoring-setup/SKILL.md` to understand the
  monitoring.md contract
- Present a plan to pauol (under 500 words): what you'll build, in what
  order, what you need from him
- Wait for "dale" before starting

During build:
- One commit per logical step
- Run the script after each meaningful change
- Ingest learnings as claims when you find something non-obvious
- Do NOT extend scope — only build M0

At end:
- Show pauol the first digest output
- Ask him to verify: does this capture the right projects? Is the format
  useful? What's missing?
- Iterate on the format before shipping M1

---

## 10. Questions pauol Will Answer

Ask these ONCE before starting, not after:

1. **projects-watched.json list**: which 3-5 projects for M0? Defaults to
   memorymaster, whatsappbot-final, wispbot unless overridden.
2. **Telegram bot token**: is the existing OpenClaw bot reused, or a new
   bot for OmniClaude? pauol will provide the token.
3. **Telegram chat_id**: confirm 2128295779 is still the target.
4. **Active hours**: 08:00-23:00 ART default — confirm or override.
5. **Dry-run first run**: default is write-to-file only (no Telegram) for
   the first 3 days, then enable Telegram. Confirm.

---

## 11. What NOT To Do

- Do NOT start with M1 (live Monitor session) before M0 is running
- Do NOT build a web UI, dashboard, or "OmniClaude admin panel"
- Do NOT introduce a database (Postgres, SQLite). Inbox is JSONL, watched
  list is JSON, claims are in MemoryMaster. That's it.
- Do NOT try to centralize execution. Inbox is centralized, execution stays
  distributed.
- Do NOT rebuild Paperclip, OpenClaw, or WispBot. They keep doing their job,
  they just start writing to inbox.jsonl.
- Do NOT skip graphify for any project. It's mandatory.
- Do NOT invent constraints that are not in this document or in MemoryMaster
  claims. If you find an edge case, ask pauol before adding a new rule.

---

**That's the handoff. Read it, query MemoryMaster for the constraint claims,
present your M0 plan, wait for "dale", ship it.**

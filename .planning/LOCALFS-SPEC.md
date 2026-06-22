# Spec — Local-Filesystem Intelligence Layer (PR1)

**Status:** DRAFT for review — no code yet.
**Source vision:** ChatGPT thread "Codex Everything Integration" (`6a38225f…`), evaluated in claim `mm-7559~3`.
**Decisions locked (this session):** spec-first; scope = `local-search` + `resolve-project` **+ ingest resolved paths** (through the sensitivity filter); governance-tuning pass deferred.
**Author:** claude-session · **Corr:** `handoff-mm4lfs1`

---

## 1. Goal & non-goals

**Goal.** Give *any* MCP/CLI agent (Claude, Codex, …) a governed way to (a) resolve a fuzzy project alias to canonical on-disk path(s) with confidence + evidence, and (b) do read-only path lookups across the machine — so agents stop crawling `D:\` / `G:\` blindly. Resolved, non-sensitive project↔path facts get remembered as governed claims so the second lookup is instant and survives across sessions and CLIs.

**Thesis.** *Everything finds the haystack, grep finds the needle, MemoryMaster remembers why the needle matters.* Three layers:

| Layer | Tool (Windows / Linux / macOS) | This PR |
|---|---|---|
| Path index | Everything ES.exe / `plocate`,`fd` / `mdfind` | ✅ EverythingProvider only; protocol leaves room for the rest |
| Content search | ripgrep / rga / Zoekt | ❌ out of scope (grep already exists for agents) |
| Governed memory | MemoryMaster claims | ✅ resolved paths ingested via `service.ingest` |

**Non-goals (explicitly deferred):**
- ❌ CSV "Everything User Values" tag-bridge — creates 3-way drift (claims ↔ CSV ↔ disk); the multi-source divergence v4 spent a whole program eliminating. Killed, not "later".
- ❌ Journal → stale-path lifecycle loop (folder moves → path claim auto-`stale`). The most MemoryMaster-native idea and the eventual crown jewel, but **SDK3-gated** (Everything 1.5 daemon, actively changing). Defer.
- ❌ A separate `futura-files` MCP server. Tools go **on the existing** `surfaces/mcp_server.py`.
- ❌ Content search / file reads / any write to the filesystem. Read-only.

---

## 2. Where the code lives (post-v4 layout — confirmed against current tree)

```
memorymaster/bridges/local_search/
    __init__.py
    provider.py        # LocalSearchProvider Protocol + dataclasses (PathHit, ResolveResult)
    everything.py      # EverythingProvider — ES.exe subprocess wrapper, read-only
    resolver.py        # resolve_project(): alias -> canonical paths + confidence + evidence
    redact.py          # path-specific sensitivity helpers (see §5)
memorymaster/surfaces/mcp_server.py     # ADD two @mcp.tool()s here (no new server)
memorymaster/surfaces/cli_handlers_basic.py  # ADD _handle_local_search / _handle_resolve_project
memorymaster/surfaces/cli_handlers_curation.py  # register both in COMMAND_HANDLERS
tests/test_local_search_provider.py
tests/test_resolve_project.py
tests/test_local_search_sensitivity.py  # red-bar-first privacy tests
```

`bridges/` already exists with `__init__.py`, `connectors/`, and peer bridges (`db_merge`, `delta_sync`, …) — `local_search/` slots in cleanly. The top-level `memorymaster/*.py` duplicates are v4 import-shims; **all new imports use subpackage paths**.

---

## 3. Surface A — `resolve-project` (the highest-value tool)

**Signature (conceptual).**
```
resolve_project(alias: str) -> {
    query: str,
    canonical_slug: str,          # via existing _canonicalize_slug()
    matches: [ { path, confidence: float, evidence: [str], source: "memory"|"everything" } ],
    best: { path, confidence } | null,
    degraded: bool,               # true if Everything unavailable -> memory-only answer
}
```

**Reuse, don't reinvent.** `surfaces/mcp_server.py` already ships `_canonicalize_slug()` (handles `- Copy`, `(1)`, `-final`/`-prod` channel folding, slugify) and `_project_scope()`. `resolve-project` is the **lookup inverse** of that logic. Algorithm:

1. `slug = _canonicalize_slug(alias)`  → reuse verbatim (extract to a shared util if importing from mcp_server is awkward).
2. **Memory first (cheap, no subprocess):** query claims in scope `project:<slug>` for prior `local_path` facts (subject/predicate convention below). A confirmed memory hit can short-circuit Everything entirely → `source:"memory"`, high confidence.
3. **Everything second:** ES query for directories whose name canonicalizes to `slug` (e.g. `ES.exe -path-column ... <alias>` then filter by `_canonicalize_slug(dirname)==slug`). Rank by: is-a-git-repo (+`.git`), has `AGENTS.md`/`CLAUDE.md`/`package.json`/`pyproject.toml`, recency, path depth, drive.
4. **Merge + score:** confidence in [0,1]; `evidence` = human-readable reasons ("matched slug", "contains pyproject.toml", "git repo", "remembered 2026-06-21").
5. **Ingest the winner** (see §4) so the next call is memory-only.

**Confidence is advisory, never authoritative** — the agent still decides. We return evidence, not a verdict.

---

## 4. The ingest half (your "PR1 + ingest" choice) — design + the one real tension

When `resolve-project` finds a confident match, write it back as a governed claim so it persists cross-session/cross-CLI:

```
svc.ingest(
    text="<project> resolves to <REDACTED-OR-SAFE path>",
    citations=[CitationInput(source="local-search", locator=slug)],
    claim_type="reference",
    subject=slug, predicate="local_path", object_value=<safe path token>,
    scope=f"project:{slug}",
    source_agent="local-search",
    confidence=<resolver confidence>,
)
```
Routing through `service.ingest` gives us the **intake policy + dedup + bitemporal fields for free** — same gate every other claim passes.

### LOCKED — option B (root-relative token)
**Decision (2026-06-21):** store paths as **root-relative tokens**. Rationale: the sensitivity rules (`.claude/rules/sensitivity-filter.md`) forbid *"home directory paths that expose usernames."* On Windows almost every project path is `C:\Users\pauol\...` or `G:\_OneDrive\...`, so a naively-ingested absolute path is both a username leak **and** gets flagged sensitive → encrypted + hidden from `query_memory`, silently defeating the memory loop.

| Opt | Store as | Verdict |
|---|---|---|
| A | full absolute path | ❌ leaks + hidden from recall |
| **B ✅** | **root-relative token** `⟨projects⟩/memorymaster`, re-expanded on read | recall works, no leak, survives folder moves |
| C | drive + last 2 segments | ❌ lossy + ambiguous on shared leaf names |

**Mechanism (~40 LOC in `redact.py`):**
- A small **roots registry**, seeded from env `MEMORYMASTER_PATH_ROOTS` (`name=path;name=path`) + auto roots (workspace parent dir, `%USERPROFILE%`). Roots tried longest-prefix-first.
- `collapse_path(roots, abspath) -> "⟨name⟩/rel/sub"` — used on **every** stored/returned path (ingest *and* tool output).
- `expand_path(roots, token) -> abspath` — used when reading a `local_path` claim back.
- If no root matches (path outside all known roots), fall through to **belt-and-suspenders**: `core.security.scan_text_for_findings(text)` before ingest and **refuse** on any finding (mirrors `_sensitive_input_error()` at the MCP `ingest_claim` boundary). So the gate is: collapse → scan → only then `service.ingest`.

---

## 5. Surface B — `local-search` (read-only path lookup)

```
local_search(query, *, limit=50, kind="any"|"dir"|"file") -> {
    hits: [ { path, kind, size?, modified? } ],   # paths collapsed via §5 redact on output too
    degraded: bool,
}
```
Thin wrapper over the provider. No ingest by default (pure lookup). Output paths run through the same `collapse_path` so a tool result never prints `C:\Users\<name>` into a transcript that may itself be ingested elsewhere.

---

## 6. `LocalSearchProvider` protocol + `EverythingProvider`

```python
class PathHit(NamedTuple):
    path: str; kind: str; size: int | None; modified: float | None

class LocalSearchProvider(Protocol):
    def available(self) -> bool: ...
    def search(self, query: str, *, limit: int, kind: str) -> list[PathHit]: ...
```

`EverythingProvider`:
- ES path from `MEMORYMASTER_EVERYTHING_ES_PATH` (no hardcoded path; `available()` = file exists + runs `-version`).
- `subprocess.run([es, ...], capture_output=True, timeout=…, shell=False)` — **never `shell=True`**, args as a list (injection-safe).
- Hard timeout (env `MEMORYMASTER_EVERYTHING_TIMEOUT`, default ~5s) + graceful degradation: any failure → `available()=False`, callers return `degraded:true` and fall back to memory-only.
- Parse ES CSV/columns into `PathHit`.
- **Linux/macOS providers are NOT built here** — the protocol just guarantees they drop in (`plocate`/`fd`, `mdfind`) without touching callers.

---

## 7. Config / env vars (all optional; sane defaults; documented in README SECURITY)

| Env | Purpose | Default |
|---|---|---|
| `MEMORYMASTER_EVERYTHING_ES_PATH` | path to `ES.exe` | unset → provider degraded |
| `MEMORYMASTER_EVERYTHING_TIMEOUT` | subprocess timeout (s) | `5` |
| `MEMORYMASTER_PATH_ROOTS` | `name=path` roots for collapse/expand (§4 opt B) | workspace parent + `%USERPROFILE%` |
| `MEMORYMASTER_LOCAL_SEARCH` | master on/off switch | `1` if ES path set |
| `MEMORYMASTER_LOCAL_SEARCH_INGEST_THRESHOLD` | min confidence to auto-ingest (§11) | `0.7` until calibrated |

---

## 8. Test plan (no Everything install required)

- **Mock subprocess** (`monkeypatch` `subprocess.run`) returns canned ES output → assert parsing, ranking, timeout handling, degraded-mode. The provider is the only subprocess touchpoint, so everything above it tests offline.
- `test_resolve_project`: alias canonicalization reuse; memory-first short-circuit; merge/scoring; evidence content.
- `test_local_search_sensitivity` (**red-bar first**): a path containing `C:\Users\<name>` or an internal IP MUST NOT be ingested verbatim — assert it's collapsed (opt B) or rejected. This is the test that proves the privacy gate, per `sensitivity-filter.md` ("every change to the filter ships with a test").
- Markers: `@pytest.mark.unit`. Run: `python -m pytest tests/ -k "local_search or resolve_project" -q`.

---

## 9. PR breakdown & acceptance

**PR1 (this spec):**
1. `bridges/local_search/` — protocol + EverythingProvider + resolver + redact.
2. Two `@mcp.tool()`s on existing server + two CLI handlers (registered in dispatch).
3. Ingest resolved paths via `service.ingest` with opt-B redaction + pre-ingest `scan_text_for_findings`.
4. Tests above green; `ruff check memorymaster/` clean; `run-cycle` still runs.

**Acceptance criteria (verifiable):**
- [ ] `resolve-project memorymaster` returns this repo's path with confidence + evidence (live, ES present).
- [ ] With ES path unset, both tools return `degraded:true` and resolve-project still answers from memory — no crash.
- [ ] A resolved path is ingested as a `reference` claim in `project:<slug>`, **re-findable by `query_memory`** (proves opt-B beat the sensitive-flag) and containing **no raw username/IP**.
- [ ] `test_local_search_sensitivity` fails if redaction is removed (intent-anchored).
- [ ] Full suite + ruff green; GitNexus `detect_changes` scope = only the new files + the two surface files.

**Deferred to later PRs (not now):** Linux/macOS providers, content-search layer, SDK3 journal→stale loop.

---

## 10. Confidence model + auto-ingest (LOCKED 2026-06-21)

**Auto-ingest on confident hits** (no `--remember` flag needed). Below threshold → resolver still *answers*, just doesn't *write*.

**Confidence = sum of explainable evidence weights** (cap 1.0). Starting weights, to be tuned by §11:

| Signal | Weight |
|---|---|
| dirname canonicalizes to exact slug | +0.40 |
| is a git repo (`.git/` present) | +0.20 |
| has a marker file (`AGENTS.md`/`CLAUDE.md`/`pyproject.toml`/`package.json`) | +0.20 |
| unambiguous (exactly one candidate matched the slug) | +0.20 |
| ambiguity penalty (N matches) | −0.10 × (N−1) |

> **As built:** a confirmed `local_path` claim in memory **short-circuits** resolution — it returns a single `source:"memory"` match at fixed confidence `0.95` and skips Everything scoring entirely (cheaper + avoids re-ranking a known answer). The earlier "+0.30 prior-memory" additive weight was removed as dead code.

Every claim's score travels with its `evidence: [str]` list, so it's auditable, never a black box.

## 11. Calibration harness — measure the threshold, don't guess it

The maintainer's `…/Py Apps/` directory is a free **labeled dataset**: dozens of real projects (memorymaster, whatsappbot-final, todomax, mzcopilot, delta-exchange, …) whose correct paths we can enumerate.

`tests/test_resolve_project_calibration.py` (marked `@pytest.mark.calibration`, skipped in CI, run on the dev box):
1. Build `{alias → known-correct-path}` by listing the projects root.
2. For each alias, run `resolve_project`; record confidence of the **correct** match and of the **best wrong** match.
3. Sweep candidate thresholds `0.5 … 0.9` (step 0.05); for each, count `correct_auto_ingests` and `wrong_auto_ingests`.
4. **Exit criterion:** choose the *lowest* threshold with `wrong_auto_ingests == 0` (maximize recall at zero false-writes). Print the table; assert such a threshold exists ≤ 0.85.
5. That number becomes the documented default for `MEMORYMASTER_LOCAL_SEARCH_INGEST_THRESHOLD` (overridable). Re-runnable any time the weight table changes — calibration is repeatable, not a one-off vibe.

Until first calibration run, default = **0.7** (placeholder, explicitly marked TODO in code).

## 12. Open questions for you
*(none blocking — §4, §10, §11 decisions are locked. Remaining items are confirm-to-build.)*

## 13. Live verification findings (2026-06-21, real ES 1.1.0.27)

Ran the full loop against the actual `es.exe` on the dev box. The mocked build had **invented the CLI interface**; reality forced several corrections (all now in code + tests):

- **Switches (verified):** `/ad` = folders only (NOT `-folder`, which errors), `/a-d` = files only, `-n <num>` = limit, `wfn:<text>` = whole-filename match. Default output = one full path per line.
- **`wfn:` is essential:** a bare substring `memorymaster` returned **261** dirs; `-n 50` truncated the real repo out entirely. Whole-name match (`whole_name=True` provider option) cuts it to ~15 and guarantees the target is in-window.
- **Scoring bug fixed:** the original uniform `−0.10×(N−1)` ambiguity penalty floored *every* confidence to 0.0 at scale. Replaced with intrinsic scoring (slug+git+marker) + a winner-only tie-damper.
- **Hidden-dir + recency:** dot-prefixed ancestors (`.gemini`, `.memorymaster`) are excluded; ties broken by most-recently-modified (active project > stale copy); `_mtime` returns 0.0 for deleted paths so stale index entries lose.
- **Happy path confirmed:** `resolve mzcopilot` → `0.80` clear winner = real repo → auto-ingested as `projects/mzcopilot` (clean token, no username/drive leak) → **2nd call is `source=memory`** (instant-lookup loop engages on our own candidate, before steward confirmation).
- **Threshold working:** `delta-exchange` (unique but no git/marker, `0.60`) correctly NOT auto-ingested.

**Known limitation (deferred to §11 calibration + a future cwd-proximity signal):** when a machine holds *multiple legitimate* copies with equal evidence (real repo + `_archive/...` copy + worktrees), the resolver returns the most-recently-modified but **does not auto-ingest** (contested → conservative). This is safe (no wrong-path pollution) but means the memory loop won't engage for such projects until calibration adds a stronger disambiguator (e.g. workspace proximity, or user confirmation). Observed live on `whatsappbot-final` (3 on-disk copies).

**Setup note:** ES CLI (`es.exe`) is a separate voidtools download from the Everything app; SDK3 is NOT needed for PR1 (subprocess only). Point `MEMORYMASTER_EVERYTHING_ES_PATH` at `es.exe`.

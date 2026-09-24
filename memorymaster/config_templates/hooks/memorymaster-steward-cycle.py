"""Scheduled task: run MemoryMaster steward cycle."""
import os
import sys

PROJECT_ROOT = "__MEMORYMASTER_PROJECT_ROOT__"
DB_PATH = os.path.join(PROJECT_ROOT, "memorymaster.db")

sys.path.insert(0, PROJECT_ROOT)
os.environ["MEMORYMASTER_DEFAULT_DB"] = DB_PATH

# LLM stack: claude_cli (Claude Code OAuth via local `claude --print`) is the
# primary, with Ollama gemma4:e4b as a defensive fallback. Direct assignment
# (NOT setdefault) — the hook MUST own these vars so an inherited shell env
# can't silently route LLM calls to a stale provider. Bug observed 2026-04-25:
# setdefault was a no-op when the inherited env already had MEMORYMASTER_LLM_PROVIDER
# set, so the new model name routed to the OLD provider → 50× HTTP 404 per cycle
# before the fallback chain saved it. Captured as v3.5.0 release notes.
os.environ["MEMORYMASTER_LLM_PROVIDER"] = "claude_cli"
os.environ["MEMORYMASTER_LLM_MODEL"] = "claude-haiku-4-5-20251001"
os.environ["MEMORYMASTER_LLM_FALLBACK_PROVIDER"] = "ollama"
os.environ["MEMORYMASTER_LLM_FALLBACK_MODEL"] = "gemma4:e4b"

# v3.13 pre-steward Jaccard dedupe — shadow mode (count would-archive but
# never act). Direct assignment so an inherited shell env can't accidentally
# flip _SHADOW=0 and start archiving without operator review.
os.environ["MEMORYMASTER_DEDUPE_ENABLED"] = "1"
os.environ["MEMORYMASTER_DEDUPE_SHADOW"] = "1"
os.environ["MEMORYMASTER_DEDUPE_JACCARD_HIGH"] = "0.85"

os.chdir(PROJECT_ROOT)

try:
    from memorymaster.core.service import MemoryService
    from pathlib import Path

    svc = MemoryService(db_target=DB_PATH, workspace_root=Path(PROJECT_ROOT))
    # batch_limit threads into the validator/extractor/deterministic/decay jobs
    # (each defaults to 200). That path is deterministic (~0 LLM calls, ~12s per
    # 200, ~107s for 2000), so a large batch is cheap and keeps the candidate
    # backlog from outgrowing the steward when many panes ingest concurrently.
    result = svc.run_cycle(batch_limit=2000)
    print(f"[MemoryMaster] steward cycle: {result}")
except Exception as e:
    print(f"[MemoryMaster] steward error: {e}", file=sys.stderr)

# S1 REVALIDATE (4.9.0): Jev re-confirms stale claims that are still right and
# useful, and records a `no_longer_useful` judgment for the rest -- the only
# thing that lets the auto-archive below retire a claim. After run_cycle (decay
# has produced this cycle's stale claims), before scheduled_archive. A no-op
# while MEMORYMASTER_JEV_MODE / MEMORYMASTER_JEV_REVALIDATE is off; capped by
# MEMORYMASTER_JEV_REVALIDATE_PER_CYCLE (default 500, 0 disables).
# Once per tenant holding stale claims (S1 selects by tenant); the per-cycle cap applies
# to each tenant, and one tenant's failure does not skip the others.
try:
    from memorymaster.govern.jobs import revalidation

    _tenants = revalidation.tenants_with_work(svc.store)
except Exception as e:
    _tenants = []
    print(f"[MemoryMaster] jev revalidation error: {e}", file=sys.stderr)
for _tenant in _tenants:
    try:
        _svc = svc if _tenant is None else MemoryService(
            db_target=DB_PATH, workspace_root=Path(PROJECT_ROOT), tenant_id=_tenant)
        reval = revalidation.run(_svc, limit=revalidation.per_cycle_limit())
        if reval.get("asked") or reval.get("stopped") not in (None, "mode_off"):
            print(f"[MemoryMaster] jev revalidation ({_tenant or 'no tenant'}): {reval}")
    except Exception as e:
        print(f"[MemoryMaster] jev revalidation error ({_tenant or 'no tenant'}): {e}", file=sys.stderr)

# S4 DEDUP (4.9.0): Jev judges candidate pairs and files `source: jev` steward
# proposals only (never a status change, never auto-approved). Here and nowhere
# else: run_cycle (MCP, CLI, per-turn operator cycle, scheduler) never asks Jev.
# A no-op while MEMORYMASTER_JEV_MODE / MEMORYMASTER_JEV_DEDUP is off; capped by
# MEMORYMASTER_JEV_DEDUP_PER_CYCLE (default 200 pairs per run, 0 disables).
# Once: the SQLite store reads candidates of every tenant, and a pair must share one.
try:
    from memorymaster.govern import candidate_dedupe

    dedup = candidate_dedupe.run_jev(svc.store, limit=candidate_dedupe.jev_pairs_per_cycle())
    if dedup.get("asked") or dedup.get("stopped") not in (None, "mode_off"):
        print(f"[MemoryMaster] jev dedup: {dedup}")
except Exception as e:
    print(f"[MemoryMaster] jev dedup error: {e}", file=sys.stderr)

# Auto-archive: stale claims never accessed, older than 14 days
try:
    from memorymaster.govern.jobs import scheduled_archive

    archive_result = scheduled_archive.run(svc, older_than_days=14)
    archived = archive_result["archived"]
    if archived:
        print(f"[MemoryMaster] auto-archived {archived} stale unused claims")
except Exception as e:
    print(f"[MemoryMaster] auto-archive error: {e}", file=sys.stderr)

# Reclaim isolated claude_cli scratch transcripts so they never grow unbounded.
# _call_claude_cli runs `claude --print` from a scratch cwd; those session
# transcripts pile up in a separate ~/.claude/projects/ folder — purge the old ones.
try:
    from memorymaster.core.llm_provider import purge_claude_cli_scratch
    _p = purge_claude_cli_scratch()
    if _p.get("removed"):
        print(f"[MemoryMaster] purged {_p['removed']} old claude_cli scratch transcripts")
except Exception as e:
    print(f"[MemoryMaster] scratch purge error: {e}", file=sys.stderr)

# Wiki layer (Obsidian markdown) — OPT-IN, default OFF (2026-07-06).
# The claims DB + FTS5 + Qdrant + entity graph + recall IS the scalable "LLM
# wiki"; the markdown vault is a redundant, non-scaling duplicate that grows
# unbounded (real install hit 2 GB / 5,921 files, hung Obsidian) and its

# Curation drain (2026-08-26, autorizado por el operador tras medir que solo 72
# de 5.976 conflictos tocaban scope=user o pinned): la maquina resuelve
# conflictos y propuestas NO-operador. Excluye pinned y scope=user SIEMPRE;
# tres veredictos deterministas (pierde->superseded, gana->promovido,
# huerfano->stale), todo con evento de auditoria y reversible. Relaja el
# "nunca aplica" de MM6 con la palabra explicita del operador.
try:
    from memorymaster.govern.jobs import curation_drain

    drain = curation_drain.run(svc, limit=1000, apply=True)
    print(f"[MemoryMaster] curation drain: {drain}")
except Exception as e:
    print(f"[MemoryMaster] curation drain error: {e}", file=sys.stderr)

# Jev decision outcomes (4.9.0): once per cycle, after every stage above that
# writes lifecycle events, join them to the decisions ledger (read-only on the
# memory DB); the ledger retention prune runs at most once a day. Creates
# nothing while Jev has never run.
try:
    from memorymaster.decisions.outcomes import steward_cycle_outcomes

    joined = steward_cycle_outcomes(DB_PATH)
    if joined.get("ledger"):
        print(f"[MemoryMaster] jev outcomes: {joined}")
except Exception as e:
    print(f"[MemoryMaster] jev outcomes error: {e}", file=sys.stderr)

# absorb runs the claude_cli stack in a loop — a heavy source of headless
# session churn. Nothing in recall depends on it (the only reader, the Closets
# stream, is default-OFF). Set MEMORYMASTER_WIKI_ABSORB=1 to enable it.
if os.environ.get("MEMORYMASTER_WIKI_ABSORB", "0").strip().lower() in ("1", "true", "yes"):
    try:
        from memorymaster.knowledge.wiki_engine import absorb
        wiki_path = os.path.join(PROJECT_ROOT, "obsidian-vault", "wiki")
        stats = absorb(DB_PATH, wiki_path)
        print(f"[MemoryMaster] wiki absorb: {stats}")
    except Exception as e:
        print(f"[MemoryMaster] wiki absorb error: {e}", file=sys.stderr)

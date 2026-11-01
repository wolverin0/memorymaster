"""Scheduled task: run MemoryMaster steward cycle."""
import os, sys

PROJECT_ROOT = r"{REPO}"
DB_PATH = os.path.join(PROJECT_ROOT, "memorymaster.db")

sys.path.append(PROJECT_ROOT)
os.environ["MEMORYMASTER_DEFAULT_DB"] = DB_PATH
# Enable v3 classifier (sound AUC 0.9924, chrono 0.5687 — beats v2 on both, claim 11894)
os.environ.setdefault("MEMORYMASTER_STEWARD_CLASSIFIER_ENABLED", "1")
os.environ.setdefault(
    "MEMORYMASTER_STEWARD_CLASSIFIER_PATH",
    os.path.join(PROJECT_ROOT, "artifacts", "steward-classifier-v3.joblib"),
)
# Cadence policy mode (MEMORYMASTER_POLICY_MODE=cadence) — real proactive revalidation.
os.environ.setdefault("MEMORYMASTER_POLICY_MODE", "cadence")
# This installation uses Gemini only, matching the operator's provider choice.
# Reuse the configured extraction model and existing Gemini API credentials.
# Direct assignment prevents inherited Claude/Ollama fallback configuration.
os.environ["MEMORYMASTER_LLM_PROVIDER"] = "google"
os.environ["MEMORYMASTER_LLM_MODEL"] = "gemini-3.5-flash-lite"
os.environ["MEMORYMASTER_LLM_FALLBACK_PROVIDER"] = ""
os.environ["MEMORYMASTER_LLM_FALLBACK_MODEL"] = ""
# Bound the selected API route using the existing per-cycle budget guard.
# Exhaustion leaves remaining work for a later cycle, never another provider.
os.environ["MEMORYMASTER_MAX_LLM_CALLS_PER_CYCLE"] = "20"
os.environ["MEMORYMASTER_MAX_TOKENS_PER_CYCLE"] = "50000"
os.environ["MEMORYMASTER_MAX_PROVIDER_FAILURES_PER_CYCLE"] = "2"
# v3.13 pre-steward Jaccard dedupe — shadow mode (count would-archive but
# never act). Direct assignment so an inherited shell env can't accidentally
# flip _SHADOW=0 and start archiving without operator review.
os.environ["MEMORYMASTER_DEDUPE_ENABLED"] = "1"
# Promoted from shadow=1 to active (shadow=0) on 2026-05-01 after
# v3.13.2 verification cycle showed 5/5 spot-check precision on real
# data (mm-716b~2). Near-dup candidates will now be archived via SQL
# without LLM intervention, with replaced_by_claim_id pointing to the
# canonical and a 'dedupe-archived: jaccard>=0.85 ...' event for audit.
os.environ["MEMORYMASTER_DEDUPE_SHADOW"] = "0"
os.environ["MEMORYMASTER_DEDUPE_JACCARD_HIGH"] = "0.85"
os.chdir(PROJECT_ROOT)

try:
    from memorymaster.service import MemoryService
    from pathlib import Path

    svc = MemoryService(db_target=DB_PATH, workspace_root=Path(PROJECT_ROOT))
    # Bumped 50 -> 200 (2026-05-10): the silent session-start-hook crash
    # had hidden a 16,988 candidate backlog. At 50/cycle that backlog
    # would've taken 85 days to clear. 200/cycle = ~21 days; combined
    # with retroactive `dedup` collapse it should drain in <1 week.
    # Gemini failures do not switch provider; leave uncertain claims pending.
    # 2026-06-06: the real throttle was the job-level batch cap (validator/
    # extractor/deterministic/decay each default limit=200), NOT policy_limit
    # (which only sizes revalidation selection). At 200/cycle the steward
    # drained slower than intake (~12 panes), so the backlog GREW to ~28k
    # (mm-7707). run_cycle now accepts batch_limit which threads into all jobs.
    # The validator/deterministic/decay path is DETERMINISTIC (≈0 LLM calls,
    # ~12s for 200), so a big batch is cheap. 2000/cycle drains the backlog in
    # a few cycles. Drop back only if a cycle runs too long under pythonw.
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

# T-0773: rapidfuzz near-duplicates among live claims (paraphrases the Jaccard
# dedupe misses). Files `source: fuzzy` steward proposals only, never a status
# change; curation_drain and automation never approve them. Off with
# MEMORYMASTER_FUZZY_DEDUP=0; capped by MEMORYMASTER_FUZZY_DEDUP_PER_CYCLE (50).
try:
    from memorymaster.govern import fuzzy_dedupe

    fuzzy = fuzzy_dedupe.run(svc.store)
    if fuzzy.get("proposed") or fuzzy.get("stopped"):
        print(f"[MemoryMaster] fuzzy dedup: {fuzzy}")
except Exception as e:
    print(f"[MemoryMaster] fuzzy dedup error: {e}", file=sys.stderr)

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
try:
    from memorymaster.core.llm_provider import purge_claude_cli_scratch
    _p = purge_claude_cli_scratch()
    if _p.get("removed"):
        print(f"[MemoryMaster] purged {_p['removed']} old claude_cli scratch transcripts")
except Exception as e:
    print(f"[MemoryMaster] scratch purge error: {e}", file=sys.stderr)


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

# Wiki layer (Obsidian markdown) — RETIRED by default 2026-07-06 (mm-f826).
# The claims DB + FTS5 + Qdrant + entity graph + recall IS the scalable "LLM
# wiki"; the markdown vault was a redundant, non-scaling duplicate that grew to
# 2GB / 5,921 files and hung Obsidian. Nothing in recall depends on it (the only
# reader, the Closets stream, is default-OFF). Re-enable the markdown view with
# MEMORYMASTER_WIKI_ABSORB=1 if you ever want it.
if os.environ.get("MEMORYMASTER_WIKI_ABSORB", "0").strip().lower() not in ("1", "true", "yes"):
    print("[MemoryMaster] wiki absorb/cleanup/breakdown DISABLED "
          "(set MEMORYMASTER_WIKI_ABSORB=1 to re-enable the Obsidian markdown view)")
    sys.exit(0)

# Wiki absorb (compiled truth + timeline articles)
try:
    # If explicitly enabled, the wiki inherits the same Gemini-only route.
    from memorymaster.wiki_engine import absorb
    wiki_path = os.path.join(PROJECT_ROOT, "obsidian-vault", "wiki")
    stats = absorb(DB_PATH, wiki_path)
    print(f"[MemoryMaster] wiki absorb: {stats}")
except Exception as e:
    print(f"[MemoryMaster] wiki absorb error: {e}", file=sys.stderr)

# Wiki cleanup + breakdown — rotate one scope per cycle so each cycle stays
# bounded (the full vault has ~30 scopes, ~28K claims, 1400+ contradictions).
# Cycle counter persists in ~/.memorymaster/steward_state.json and advances
# one scope every run, so over ~30 cycles (~7.5 days at 6h cadence) the
# whole vault gets cleanup + breakdown coverage. Without this rotation, the
# steward would burn the entire claude_cli rate limit on a single cycle.
try:
    import json
    from pathlib import Path as _Path
    from memorymaster.wiki_engine import cleanup as _wiki_cleanup, breakdown as _wiki_breakdown

    state_dir = _Path(os.path.expanduser("~")) / ".memorymaster"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_dir / "steward_state.json"
    state = {}
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            state = {}

    wiki_path = os.path.join(PROJECT_ROOT, "obsidian-vault", "wiki")
    scopes = sorted(
        d.name for d in _Path(wiki_path).iterdir()
        if d.is_dir() and not d.name.startswith("_") and d.name != "bases"
    )
    if scopes:
        idx = int(state.get("scope_cursor", 0)) % len(scopes)
        target = scopes[idx]
        state["scope_cursor"] = (idx + 1) % len(scopes)
        state["last_cleanup_scope"] = target
        state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")

        # Map dir name back to canonical scope: project-foo -> project:foo
        canonical = target.replace("project-", "project:", 1) if target.startswith("project-") else target
        try:
            cleanup_stats = _wiki_cleanup(wiki_path, scope_filter=canonical)
            print(f"[MemoryMaster] wiki cleanup ({canonical}): {cleanup_stats}")
        except Exception as e:
            print(f"[MemoryMaster] wiki cleanup error ({canonical}): {e}", file=sys.stderr)
        try:
            breakdown_stats = _wiki_breakdown(DB_PATH, wiki_path, scope_filter=canonical)
            print(f"[MemoryMaster] wiki breakdown ({canonical}): {breakdown_stats}")
        except Exception as e:
            print(f"[MemoryMaster] wiki breakdown error ({canonical}): {e}", file=sys.stderr)
except Exception as e:
    print(f"[MemoryMaster] wiki rotation error: {e}", file=sys.stderr)

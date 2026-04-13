from __future__ import annotations

import argparse
from pathlib import Path

from memorymaster.cli_helpers import (  # noqa: F401 — re-export for backward compat with tests/external callers
    STEALTH_DB_NAME,
    _add_cycle_policy_args,
    _json_error,
    _resolve_claim_id,
    _resolve_db_path,
    _stealth_active,
    parse_citation,
    parse_scope_allowlist,
)
from memorymaster.context_optimizer import OUTPUT_FORMATS
from memorymaster.models import CLAIM_LINK_TYPES, CLAIM_STATUSES, VOLATILITY_LEVELS
from memorymaster.retrieval import RETRIEVAL_MODES
from memorymaster.service import MemoryService

# Import dispatch table — this also triggers the late dispatch additions for daily/dream/ghost
from memorymaster.cli_handlers_curation import COMMAND_HANDLERS


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memorymaster", description="Memory reliability MVP CLI")
    parser.add_argument("--json", "-j", action="store_true", dest="json_output", help="Output machine-readable JSON instead of human-readable text")
    parser.add_argument("--db", default="memorymaster.db", help="SQLite path or Postgres DSN (postgresql://...)")
    parser.add_argument("--workspace", default=".", help="Workspace root used for deterministic codebase checks and git-triggered scheduling")
    parser.add_argument("--stealth", action="store_true", help="Use local-only stealth DB (.memorymaster-stealth.db) in the current directory")
    parser.add_argument("--tenant", default=None, help="Tenant ID for multi-tenant isolation (only claims with this tenant_id are visible)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="Create schema in SQLite database")

    sub.add_parser("stealth-status", help="Show whether stealth mode is active and which DB is in use")

    ingest = sub.add_parser("ingest", help="Ingest a raw claim with citations")
    ingest.add_argument("--text", required=True, help="Claim text")
    ingest.add_argument("--source", action="append", default=[], help="Citation in format source|locator|excerpt (repeat for multiple citations)")
    ingest.add_argument("--claim-type", help="Optional claim type label")
    ingest.add_argument("--subject", help="Optional claim subject")
    ingest.add_argument("--predicate", help="Optional claim predicate")
    ingest.add_argument("--object", dest="object_value", help="Optional claim object/value")
    ingest.add_argument("--idempotency-key", help="Optional key to dedupe ingest retries")
    ingest.add_argument("--scope", default="project", help="Claim scope (default: project)")
    ingest.add_argument("--volatility", choices=list(VOLATILITY_LEVELS), default="medium")
    ingest.add_argument("--confidence", type=float, default=0.5, help="Initial confidence (0-1)")
    ingest.add_argument("--event-time", default=None, help="ISO-8601 timestamp: when the fact occurred in the real world")
    ingest.add_argument("--valid-from", default=None, help="ISO-8601 timestamp: start of the claim validity window")
    ingest.add_argument("--valid-until", default=None, help="ISO-8601 timestamp: end of the validity window (omit if still current)")

    cycle = sub.add_parser("run-cycle", help="Run extractor, validator, decay, and optional compact")
    cycle.add_argument("--with-compact", action="store_true", help="Run compactor at the end of cycle")
    cycle.add_argument("--with-dream-sync", action="store_true", help="Sync claims with Claude Code Auto Dream after cycle")
    cycle.add_argument("--dream-project", default=None, help="Project path for Auto Dream sync (or set CLAUDE_MEMORY_DIR)")
    _add_cycle_policy_args(cycle)

    query = sub.add_parser("query", help="Search claims by text")
    query.add_argument("text", help="Query text")
    query.add_argument("--limit", type=int, default=20, help="Maximum rows")
    query.add_argument("--exclude-stale", action="store_true", help="Only return confirmed/conflicted")
    query.add_argument("--exclude-conflicted", action="store_true", help="Only return confirmed/stale")
    query.add_argument("--include-candidates", action="store_true", help="Also search candidate (unverified) claims")
    query.add_argument("--retrieval-mode", choices=list(RETRIEVAL_MODES), default="legacy", help="Retrieval mode (legacy SQL ordering or hybrid lexical/confidence/freshness ranking)")
    query.add_argument("--allow-sensitive", action="store_true", help="Include claims that look sensitive (default excludes them)")
    query.add_argument("--scope-allowlist", default="", help="Comma-separated scopes to include (e.g. project,team_x)")
    query.add_argument("--as-of", default="", help="Temporal query: show claims valid at this ISO timestamp")
    query.add_argument("--auto-classify", action="store_true", help="Auto-classify query type and use optimal retrieval mode")

    context = sub.add_parser("context", help="Pack relevant claims into a token-budgeted context block for AI agents")
    context.add_argument("text", help="Query text describing what context is needed")
    context.add_argument("--budget", type=int, default=4000, help="Maximum token budget (default: 4000)")
    context.add_argument("--format", dest="output_format", choices=list(OUTPUT_FORMATS), default="text", help="Output format: text (human-readable), xml (system prompt), json (structured)")
    context.add_argument("--limit", type=int, default=100, help="Max candidate claims to rank")
    context.add_argument("--exclude-stale", action="store_true", help="Exclude stale claims")
    context.add_argument("--exclude-conflicted", action="store_true", help="Exclude conflicted claims")
    context.add_argument("--include-candidates", action="store_true", help="Include candidate (unverified) claims")
    context.add_argument("--retrieval-mode", choices=list(RETRIEVAL_MODES), default="hybrid", help="Retrieval mode (default: hybrid)")
    context.add_argument("--allow-sensitive", action="store_true", help="Include sensitive claims")
    context.add_argument("--scope-allowlist", default="", help="Comma-separated scopes to include")

    pin = sub.add_parser("pin", help="Pin or unpin a claim")
    pin.add_argument("claim_id", help="Claim numeric id or human_id (e.g. mm-a3f8)")
    pin.add_argument("--unpin", action="store_true", help="Unpin instead of pinning")

    redact_claim = sub.add_parser("redact-claim", help="Non-destructive redact/erase workflow for claim payload")
    redact_claim.add_argument("claim_id", help="Claim numeric id or human_id (e.g. mm-a3f8)")
    redact_claim.add_argument("--mode", choices=["redact", "erase"], default="redact", help="Workflow mode")
    redact_target = redact_claim.add_mutually_exclusive_group()
    redact_target.add_argument("--claims-only", action="store_true", help="Only scrub claim fields")
    redact_target.add_argument("--citations-only", action="store_true", help="Only scrub citation fields")
    redact_claim.add_argument("--reason", default="", help="Optional audit reason")
    redact_claim.add_argument("--actor", default="cli", help="Audit source value")

    compact = sub.add_parser("compact", help="Archive stale/superseded/conflicted claims and trim old events")
    compact.add_argument("--retain-days", type=int, default=30, help="Days before archiving stale/superseded/conflicted claims")
    compact.add_argument("--event-retain-days", type=int, default=60, help="Days to retain event history")

    compact_sum = sub.add_parser("compact-summaries", help="Summarize groups of archived claims into higher-level summary claims using LLM")
    compact_sum.add_argument("--provider", default="gemini", choices=["gemini", "openai", "anthropic", "ollama", "custom"], help="LLM provider (default: gemini)")
    compact_sum.add_argument("--api-key", default="", help="API key for the LLM provider")
    compact_sum.add_argument("--api-keys", default="", help="Comma-separated API keys for round-robin rotation")
    compact_sum.add_argument("--model", default="", help="Model name (uses provider default if omitted)")
    compact_sum.add_argument("--base-url", default="", help="Custom API base URL")
    compact_sum.add_argument("--min-cluster", type=int, default=3, help="Minimum claims per cluster to trigger summarization (default: 3)")
    compact_sum.add_argument("--max-cluster", type=int, default=20, help="Maximum claims per cluster before splitting (default: 20)")
    compact_sum.add_argument("--similarity-threshold", type=float, default=0.65, help="Cosine similarity threshold for embedding-based clustering (default: 0.65)")
    compact_sum.add_argument("--limit", type=int, default=500, help="Maximum archived claims to consider (default: 500)")
    compact_sum.add_argument("--cooldown", type=float, default=60.0, help="Cooldown seconds for rate-limited keys (default: 60)")
    compact_sum.add_argument("--dry-run", action="store_true", help="Preview clusters without creating summaries")

    dedup = sub.add_parser("dedup", help="Detect and merge duplicate claims using embedding similarity")
    dedup.add_argument("--threshold", type=float, default=0.92, help="Cosine similarity threshold for duplicate detection (default: 0.92)")
    dedup.add_argument("--min-text-overlap", type=float, default=0.3, help="Minimum word-level Jaccard overlap as secondary gate (default: 0.3)")
    dedup.add_argument("--dry-run", action="store_true", help="Preview duplicates without archiving")

    sub.add_parser("recompute-tiers", help="Recompute memory tiers (core/working/peripheral) for all claims")

    list_claims = sub.add_parser("list-claims", help="List claims")
    list_claims.add_argument("--status", choices=list(CLAIM_STATUSES), help="Filter by claim status")
    list_claims.add_argument("--limit", type=int, default=50, help="Maximum rows")
    list_claims.add_argument("--include-archived", action="store_true", help="Include archived claims")
    list_claims.add_argument("--allow-sensitive", action="store_true", help="Include claims that look sensitive (default excludes them)")

    list_events = sub.add_parser("list-events", help="List events")
    list_events.add_argument("--claim-id", type=int, help="Filter by claim id")
    list_events.add_argument("--event-type", help="Filter by event type")
    list_events.add_argument("--limit", type=int, default=100, help="Maximum rows")

    history = sub.add_parser("history", help="Full audit trail timeline for a single claim")
    history.add_argument("claim_id", help="Claim numeric id or human_id (e.g. mm-a3f8)")
    history.add_argument("--limit", type=int, default=50, help="Maximum events to show")

    export_metrics = sub.add_parser("export-metrics", help="Export D3 structured metrics from JSONL events")
    export_metrics.add_argument("--events-jsonl", action="append", required=True, help="Path to JSONL events input (repeat flag for multiple files)")
    export_metrics.add_argument("--out-prom", default="artifacts/metrics/metrics.prom", help="Output path for Prometheus text metrics")
    export_metrics.add_argument("--out-json", default="artifacts/metrics/metrics_snapshot.json", help="Output path for structured metrics JSON snapshot")

    review_queue = sub.add_parser("review-queue", help="Build conflict/stale review queue")
    review_queue.add_argument("--limit", type=int, default=100, help="Maximum claims scanned for queue")
    review_queue.add_argument("--exclude-stale", action="store_true", help="Exclude stale claims from queue")
    review_queue.add_argument("--exclude-conflicted", action="store_true", help="Exclude conflicted claims from queue")
    review_queue.add_argument("--allow-sensitive", action="store_true", help="Include claims that look sensitive (default excludes them)")

    daemon = sub.add_parser("run-daemon", help="Run scheduler loop for periodic/background memory maintenance")
    daemon.add_argument("--interval-seconds", type=int, default=3600, help="Timer-based cycle interval")
    daemon.add_argument("--max-cycles", type=int, help="Exit after N cycles")
    daemon.add_argument("--compact-every", type=int, default=0, help="Run compactor every N cycles (0 disables)")
    _add_cycle_policy_args(daemon)
    daemon.add_argument("--git-trigger", action="store_true", help="Run cycle when git HEAD changes")
    daemon.add_argument("--git-check-seconds", type=int, default=10, help="How often to poll git HEAD")

    dashboard = sub.add_parser("run-dashboard", help="Run read-only HTTP dashboard/API")
    dashboard.add_argument("--host", default="127.0.0.1", help="Bind host")
    dashboard.add_argument("--port", type=int, default=8765, help="Bind port")
    dashboard.add_argument("--operator-log-jsonl", default="artifacts/operator/operator_events.jsonl", help="Path consumed by /api/operator/stream")

    operator = sub.add_parser("run-operator", help="Run pre/post-turn memory maintenance loop from JSONL inbox")
    operator.add_argument("--inbox-jsonl", required=True, help="Path to JSONL turn-event inbox")
    operator.add_argument("--poll-seconds", type=float, default=1.0, help="Polling interval for inbox tailing")
    operator.add_argument("--max-events", type=int, help="Exit after processing N turn events")
    operator.add_argument("--max-idle-seconds", type=float, default=0.0, help="Exit if no new inbox lines arrive for N seconds (0 disables)")
    operator.add_argument("--reconcile-seconds", type=float, default=300, help="Periodic reconciliation interval for background maintenance")
    operator.add_argument("--retrieval-mode", choices=list(RETRIEVAL_MODES), default="hybrid", help="Retrieval mode for pre-turn memory query")
    operator.add_argument("--query-limit", type=int, default=8, help="Max claims to fetch during pre-turn retrieval")
    operator.add_argument("--disable-progressive-retrieval", action="store_true", help="Use a single retrieval query instead of progressive tiered retrieval")
    operator.add_argument("--tier1-limit", type=int, default=4, help="Tier-1 retrieval limit when progressive retrieval is enabled")
    operator.add_argument("--tier2-limit", type=int, default=8, help="Tier-2 retrieval limit when progressive retrieval falls back")
    _add_cycle_policy_args(operator, policy_default="cadence")
    operator.add_argument("--compact-every", type=int, default=0, help="Run compactor every N processed turns")
    operator.add_argument("--log-jsonl", default="artifacts/operator/operator_events.jsonl", help="JSONL path for operator run events (empty disables logging)")
    operator.add_argument("--state-json", default="artifacts/operator/operator_state.json", help="JSON path for operator checkpoint state (empty disables state persistence)")
    operator.add_argument("--queue-state-json", default="artifacts/operator/operator_queue_state.json", help="JSON path for durable pending queue state (empty disables durable queue state persistence)")
    operator.add_argument("--queue-journal-jsonl", default="artifacts/operator/operator_queue_journal.jsonl", help="JSONL append-only journal path for durable queue enqueue/ack events (empty disables queue journal)")
    operator.add_argument("--queue-db", default="", help="SQLite WAL database path for crash-safe pending queue (empty uses legacy JSON persistence)")
    operator.add_argument("--no-state", action="store_true", help="Disable checkpoint and durable queue state load/save")

    steward = sub.add_parser("run-steward", help="Run claim stewardship probes and proposal generation")
    steward.add_argument("--mode", choices=["manual", "cadence"], default="manual", help="Loop mode")
    steward.add_argument("--cadence-trigger", choices=["timer", "commit", "timer_or_commit"], default="timer", help="Cadence trigger strategy when mode=cadence")
    steward.add_argument("--interval-seconds", type=float, default=30.0, help="Sleep interval between cadence cycles")
    steward.add_argument("--git-check-seconds", type=float, default=10.0, help="Git polling interval for commit-triggered cadence")
    steward.add_argument("--commit-every", type=int, default=1, help="Run a stewardship cycle after N observed git head changes")
    steward.add_argument("--max-cycles", type=int, default=1, help="Number of cycles to run")
    steward.add_argument("--max-claims", type=int, default=200, help="Max claims scanned per cycle")
    steward.add_argument("--max-proposals", type=int, default=200, help="Max proposal events emitted per cycle")
    steward.add_argument("--max-probe-files", type=int, default=200, help="Max files scanned for filesystem probe")
    steward.add_argument("--max-probe-file-bytes", type=int, default=524288, help="Skip files larger than this byte size during filesystem probe")
    steward.add_argument("--max-tool-probes", type=int, default=200, help="Maximum tool probe executions per cycle")
    steward.add_argument("--probe-timeout-seconds", type=float, default=2.0, help="Per-probe timeout budget in seconds")
    steward.add_argument("--probe-failure-threshold", type=int, default=3, help="Open circuit breaker for a probe type after this many timeout/error failures")
    steward.add_argument("--disable-semantic-probe", action="store_true", help="Disable semantic retrieval probe in steward planner")
    steward.add_argument("--disable-tool-probe", action="store_true", help="Disable tool/storage probe in steward planner")
    steward.add_argument("--allow-sensitive", action="store_true", help="Include sensitive claims in stewardship scan")
    steward.add_argument("--apply", action="store_true", help="Apply proposed status transitions")
    steward.add_argument("--artifact-json", default="artifacts/steward/steward_report.json", help="Path to steward JSON report artifact")

    steward_proposals = sub.add_parser("steward-proposals", help="List steward proposal events for human override")
    steward_proposals.add_argument("--limit", type=int, default=100, help="Maximum proposals returned")
    steward_proposals.add_argument("--include-resolved", action="store_true", help="Include already approved/rejected proposals")

    resolve_proposal = sub.add_parser("resolve-proposal", help="Approve or reject steward proposal")
    resolve_proposal.add_argument("--action", choices=["approve", "reject"], required=True, help="Resolution action")
    resolve_proposal.add_argument("--proposal-event-id", type=int, help="Specific steward proposal event id")
    resolve_proposal.add_argument("--claim-id", type=int, help="Resolve latest pending proposal for claim id")
    resolve_proposal.add_argument("--no-apply", action="store_true", help="When approving, do not apply state transition; only mark proposal approved")

    link_cmd = sub.add_parser("link", help="Create a typed link between two claims")
    link_cmd.add_argument("source_id", help="Source claim numeric id or human_id")
    link_cmd.add_argument("target_id", help="Target claim numeric id or human_id")
    link_cmd.add_argument("--type", dest="link_type", choices=list(CLAIM_LINK_TYPES), default="relates_to", help="Link type (default: relates_to)")

    unlink_cmd = sub.add_parser("unlink", help="Remove link(s) between two claims")
    unlink_cmd.add_argument("source_id", help="Source claim numeric id or human_id")
    unlink_cmd.add_argument("target_id", help="Target claim numeric id or human_id")
    unlink_cmd.add_argument("--type", dest="link_type", choices=list(CLAIM_LINK_TYPES), default=None, help="Remove only this link type (default: remove all links between the pair)")

    links_cmd = sub.add_parser("links", help="Show all links for a claim")
    links_cmd.add_argument("claim_id", help="Claim numeric id or human_id")
    links_cmd.add_argument("--type", dest="link_type", choices=list(CLAIM_LINK_TYPES), default=None, help="Filter by link type")

    resolve_conflicts_cmd = sub.add_parser("resolve-conflicts", help="Detect and auto-resolve conflicting claims (same subject+predicate, different object_value)")
    resolve_conflicts_cmd.add_argument("--dry-run", action="store_true", help="Detect conflicts but do not apply transitions")
    resolve_conflicts_cmd.add_argument("--limit", type=int, default=500, help="Maximum claims to scan for conflicts")

    staleness_cmd = sub.add_parser("check-staleness", help="Detect claims whose cited source files have changed and flag them stale")
    staleness_cmd.add_argument("--mode", choices=["mtime", "git"], default="mtime", help="Detection mode: mtime (file modification time) or git (git log)")
    staleness_cmd.add_argument("--dry-run", action="store_true", help="Detect stale claims but do not apply transitions")
    staleness_cmd.add_argument("--limit", type=int, default=500, help="Maximum claims to scan per status")

    ready_cmd = sub.add_parser("ready", help="Show claims needing attention: stale, conflicted, and low-confidence candidates")
    ready_cmd.add_argument("--limit", type=int, default=10, help="Maximum claims per category (default: 10)")
    ready_cmd.add_argument("--confidence-threshold", type=float, default=0.5, help="Confidence threshold for low-confidence candidates (default: 0.5)")

    snap = sub.add_parser("snapshot", help="Create a versioned snapshot of the claim DB")
    snap.add_argument("--message", "-m", default="", help="Optional description for this snapshot")

    sub.add_parser("snapshots", help="List all DB snapshots with dates and commit hashes")

    rb = sub.add_parser("rollback", help="Restore the DB from a snapshot (creates a safety backup first)")
    rb.add_argument("snapshot_id", help="Snapshot ID (or unambiguous prefix)")
    rb.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")

    snap_diff = sub.add_parser("diff", help="Show claims added/removed/changed since a snapshot")
    snap_diff.add_argument("snapshot_id", help="Snapshot ID (or unambiguous prefix)")

    sub.add_parser("install-hook", help="Install a git post-commit hook that auto-snapshots the DB")

    qdrant_sync = sub.add_parser("qdrant-sync", help="Bulk-sync all active claims to Qdrant vector store")
    qdrant_sync.add_argument("--qdrant-url", default="", help="Qdrant endpoint (default: $QDRANT_URL or localhost:6333)")
    qdrant_sync.add_argument("--ollama-url", default="", help="Ollama endpoint (default: $OLLAMA_URL or localhost:11434)")

    qdrant_search = sub.add_parser("qdrant-search", help="Semantic search via Qdrant vector store")
    qdrant_search.add_argument("text", help="Query text for semantic search")
    qdrant_search.add_argument("--limit", type=int, default=5, help="Max results (default: 5)")
    qdrant_search.add_argument("--min-confidence", type=float, default=0.0, help="Minimum confidence filter")
    qdrant_search.add_argument("--states", default="", help="Comma-separated state filter (e.g. confirmed,stale)")
    qdrant_search.add_argument("--qdrant-url", default="", help="Qdrant endpoint")
    qdrant_search.add_argument("--ollama-url", default="", help="Ollama endpoint")

    vault = sub.add_parser("export-vault", help="Export claims as Obsidian-compatible .md files")
    vault.add_argument("--output", required=True, help="Output directory for .md files")
    vault.add_argument("--scope", default="", help="Only export claims matching this scope prefix")
    vault.add_argument("--confirmed-only", action="store_true", help="Only export confirmed claims")
    vault.add_argument("--include-archived", action="store_true", help="Include archived claims")
    vault.add_argument("--incremental", action="store_true", help="Only export claims changed since last export")

    curate = sub.add_parser("curate-vault", help="LLM-curated Obsidian vault with topics and wikilinks")
    curate.add_argument("--output", required=True, help="Output directory for curated vault")
    curate.add_argument("--scope", default="", help="Only curate claims matching this scope prefix")
    curate.add_argument("--dry-run", action="store_true", help="Show topic breakdown without writing files")

    lint = sub.add_parser("lint-vault", help="Detect contradictions, orphans, gaps, and stale claims")
    lint.add_argument("--scope", default="", help="Only lint claims matching this scope prefix")
    lint.add_argument("--no-llm", action="store_true", help="Skip LLM verification of contradictions")
    lint.add_argument("--max-stale-days", type=int, default=30, help="Max age in days before flagging as stale")

    wiki_absorb = sub.add_parser("wiki-absorb", help="Absorb claims into wiki articles (Karpathy/Farza style)")
    wiki_absorb.add_argument("--output", default="obsidian-vault", help="Wiki directory")
    wiki_absorb.add_argument("--scope", default="", help="Scope filter")
    wiki_absorb.add_argument("--no-bases", action="store_true", help="Skip regenerating Obsidian Bases")

    bases_generate = sub.add_parser("bases-generate", help="Regenerate Obsidian Bases (.base) files for the wiki")
    bases_generate.add_argument("--output", default="obsidian-vault", help="Vault root (writes to <root>/bases/)")

    wiki_cleanup = sub.add_parser("wiki-cleanup", help="Audit and rewrite weak wiki articles")
    wiki_cleanup.add_argument("--output", default="obsidian-vault", help="Wiki directory")
    wiki_cleanup.add_argument("--scope", default="", help="Scope filter")

    wiki_breakdown = sub.add_parser("wiki-breakdown", help="Find and create missing wiki articles")
    wiki_breakdown.add_argument("--output", default="obsidian-vault", help="Wiki directory")
    wiki_breakdown.add_argument("--scope", default="", help="Scope filter")

    wiki_backfill = sub.add_parser(
        "wiki-backfill-bindings",
        help="Backfill claims.wiki_article from existing wiki article frontmatter (v3.4)",
    )
    wiki_backfill.add_argument("--output", default="obsidian-vault", help="Wiki directory to scan")

    mine_cmd = sub.add_parser("mine-transcript", help="Parse Claude Code transcripts into claims")
    mine_cmd.add_argument("--input", required=True, help="JSONL transcript file or directory")
    mine_cmd.add_argument("--scope", default="project", help="Scope for ingested claims")
    mine_cmd.add_argument("--max", type=int, default=100, help="Max claims to ingest")

    verify_cmd = sub.add_parser("verify-claims", help="Cross-check claims against current codebase")
    verify_cmd.add_argument("--scope", default="", help="Scope filter")
    verify_cmd.add_argument("--limit", type=int, default=200, help="Max claims to check")

    entity_cmd = sub.add_parser("extract-entities", help="Run entity extraction on claims via LLM")
    entity_cmd.add_argument("--limit", type=int, default=100, help="Max claims to process")
    entity_cmd.add_argument("--status", default="confirmed", help="Only process claims with this status")

    sub.add_parser("entity-stats", help="Show entity graph statistics")

    sub.add_parser("feedback-stats", help="Show feedback tracking and quality score statistics")

    sub.add_parser("quality-scores", help="Recompute quality scores for all claims")

    resolve_cmd = sub.add_parser("auto-resolve", help="Use LLM to resolve conflicted claims")
    resolve_cmd.add_argument("--limit", type=int, default=20, help="Max conflict pairs to evaluate")

    sub.add_parser("train-model", help="Train quality prediction model from feedback data (requires sklearn)")

    extract_claims_cmd = sub.add_parser("extract-claims", help="Extract structured claims from unstructured text using LLM")
    extract_claims_input = extract_claims_cmd.add_mutually_exclusive_group(required=True)
    extract_claims_input.add_argument("--input", metavar="FILE_OR_TEXT", help="Path to a text file, or raw text string to extract from")
    extract_claims_input.add_argument("--stdin", action="store_true", help="Read text from stdin")
    extract_claims_cmd.add_argument("--source", default="unstructured", help="Citation source label (default: unstructured)")
    extract_claims_cmd.add_argument("--scope", default="project", help="Claim scope (default: project)")
    extract_claims_cmd.add_argument("--ingest", action="store_true", help="Ingest extracted claims into the DB (default: dry-run, print only)")
    extract_claims_cmd.add_argument("--ollama-url", default="", help="Ollama base URL (default: $OLLAMA_URL or http://localhost:11434)")
    extract_claims_cmd.add_argument("--model", default="", help="LLM model name (default: deepseek-coder-v2:16b)")

    fed_query = sub.add_parser("federated-query", help="Query across ALL scopes — cross-project federation")
    fed_query.add_argument("text", help="Query text")
    fed_query.add_argument("--limit", type=int, default=20, help="Maximum rows (default: 20)")

    sub.add_parser("sessions", help="List active and recent agent sessions")

    sub.add_parser("install-gitnexus-hook", help="Install GitNexus post-commit hook that re-analyzes the project after each commit")

    recall_cmd = sub.add_parser("recall", help="Query memory for relevant context (for pre-turn injection)")
    recall_cmd.add_argument("query", help="What context do you need?")
    recall_cmd.add_argument("--budget", type=int, default=2000, help="Token budget for context")
    recall_cmd.add_argument("--format", dest="output_format", default="text", choices=["text", "xml", "json"], help="Output format")

    observe_cmd = sub.add_parser("observe", help="Extract and ingest observations from text (for post-turn learning)")
    observe_cmd.add_argument("--text", required=True, help="Text to observe and potentially ingest")
    observe_cmd.add_argument("--source", default="session", help="Source label")
    observe_cmd.add_argument("--scope", default="project", help="Claim scope")
    observe_cmd.add_argument("--force", action="store_true", help="Ingest even if no pattern match")
    observe_cmd.add_argument("--llm", action="store_true", help="Use LLM for deeper extraction (slower)")

    merge_cmd = sub.add_parser("merge-db", help="Merge claims from a remote memorymaster DB (bidirectional sync)")
    merge_cmd.add_argument("--source", required=True, help="Path to source DB file to merge from")

    daily = sub.add_parser("daily-note", help="Generate a daily note summarizing today's activity")
    daily.add_argument("--date", default="", help="Date to generate for (YYYY-MM-DD, default: today)")
    daily.add_argument("--output", default="", help="Directory to save .md file (default: print to stdout)")

    sub.add_parser("ghost-notes", help="Find knowledge gaps — topics queried often but with few claims")

    dream_seed = sub.add_parser("dream-seed", help="Export MemoryMaster claims into Claude Code Auto Dream memory files")
    dream_seed.add_argument("--project", default=None, help="Project path to compute Claude Code memory dir slug")
    dream_seed.add_argument("--min-tier", type=int, default=2, help="Minimum tier to export (1=core, 2=working, 3=peripheral; default: 2)")
    dream_seed.add_argument("--min-quality", type=float, default=0.5, help="Minimum quality score to export (default: 0.5)")
    dream_seed.add_argument("--max", type=int, default=50, help="Maximum memory files to create (default: 50)")
    dream_seed.add_argument("--dry-run", action="store_true", help="Preview what would be exported without writing files")

    dream_ingest_cmd = sub.add_parser("dream-ingest", help="Import Auto Dream memories back into MemoryMaster")
    dream_ingest_cmd.add_argument("--project", default=None, help="Project path to compute Claude Code memory dir slug")

    dream_sync_cmd = sub.add_parser("dream-sync", help="Bidirectional sync between MemoryMaster and Auto Dream")
    dream_sync_cmd.add_argument("--project", default=None, help="Project path to compute Claude Code memory dir slug")
    dream_sync_cmd.add_argument("--min-tier", type=int, default=2, help="Minimum tier to export (default: 2)")
    dream_sync_cmd.add_argument("--min-quality", type=float, default=0.5, help="Minimum quality score to export (default: 0.5)")
    dream_sync_cmd.add_argument("--max", type=int, default=50, help="Maximum memory files to create (default: 50)")

    dream_clean_cmd = sub.add_parser("dream-clean", help="Remove all mm_-prefixed files from Claude Code memory dir")
    dream_clean_cmd.add_argument("--project", default=None, help="Project path to compute Claude Code memory dir slug")
    dream_clean_cmd.add_argument("--dry-run", action="store_true", help="Preview what would be removed without deleting files")

    # Entity registry (GBrain-inspired)
    entity_list = sub.add_parser("entity-list", help="List canonical entities with alias and claim counts")
    entity_list.add_argument("--scope", default="", help="Filter by scope prefix")
    entity_list.add_argument("--type", default="", help="Filter by entity type")
    entity_list.add_argument("--limit", type=int, default=50)

    entity_merge = sub.add_parser("entity-merge", help="Merge two entities (move aliases + claims to target)")
    entity_merge.add_argument("keep_id", type=int, help="Entity ID to keep")
    entity_merge.add_argument("merge_id", type=int, help="Entity ID to merge into keep_id")

    entity_aliases_cmd = sub.add_parser("entity-aliases", help="List or add aliases for an entity")
    entity_aliases_cmd.add_argument("entity_id", type=int, help="Entity ID")
    entity_aliases_cmd.add_argument("--add", default="", help="Add this alias to the entity")

    entity_backfill = sub.add_parser("entity-backfill", help="Backfill entity_id on claims with subject but no entity")

    return parser



def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    effective_db = _resolve_db_path(args)

    # Commands that don't need MemoryService run first; service is lazy-created once for all others.
    _NO_SERVICE_COMMANDS = {"stealth-status", "export-metrics"}

    try:
        handler = COMMAND_HANDLERS.get(args.command)
        if handler is None:
            parser.print_help()
            return 1

        if args.command in _NO_SERVICE_COMMANDS:
            return handler(args, None, parser, effective_db)

        service = MemoryService(effective_db, workspace_root=Path(args.workspace),
            tenant_id=getattr(args, "tenant", None))
        return handler(args, service, parser, effective_db)

    except Exception as exc:
        if args.json_output:
            print(_json_error(str(exc)))
        else:
            print(f"error: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

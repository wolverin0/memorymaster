from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass
import json
import os
from pathlib import Path
import time

from memorymaster.models import CLAIM_LINK_TYPES, CLAIM_STATUSES, CitationInput, VOLATILITY_LEVELS
from memorymaster.policy import POLICY_MODES
from memorymaster.context_optimizer import OUTPUT_FORMATS
from memorymaster.retrieval import RETRIEVAL_MODES
from memorymaster.scheduler import run_daemon
from memorymaster.security import resolve_allow_sensitive_access
from memorymaster.service import MemoryService

STEALTH_DB_NAME = ".memorymaster-stealth.db"
_SCORE_KEYS = ("score", "lexical_score", "confidence_score", "freshness_score", "vector_score")


def parse_citation(raw: str) -> CitationInput:
    # Format: source|locator|excerpt (locator/excerpt optional).
    parts = [part.strip() for part in raw.split("|", 2)]
    source = parts[0] if parts else ""
    if not source:
        raise ValueError("Citation source is required.")
    locator = parts[1] if len(parts) > 1 and parts[1] else None
    excerpt = parts[2] if len(parts) > 2 and parts[2] else None
    return CitationInput(source=source, locator=locator, excerpt=excerpt)


def parse_scope_allowlist(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    return [part.strip() for part in raw.split(",") if part.strip()] or None


def _claim_to_dict(claim) -> dict:
    """Serialize a Claim dataclass to a plain dict for JSON output."""
    return asdict(claim) if is_dataclass(claim) else dict(claim)


def _json_envelope(data, *, total: int | None = None, query_ms: float) -> str:
    """Format the standard JSON envelope for --json output."""
    meta: dict = {"query_ms": round(query_ms, 2), **({"total": total} if total is not None else {})}
    return json.dumps({"ok": True, "data": data, "meta": meta}, indent=2, default=_json_default)


def _json_error(message: str) -> str:
    """Format a JSON error envelope."""
    return json.dumps({"ok": False, "error": str(message)})


def _resolve_claim_id(service: MemoryService, raw: str | int) -> int:
    """Resolve a CLI claim identifier (numeric or human_id) to an integer ID."""
    if isinstance(raw, int):
        return raw
    text = str(raw).strip()
    try:
        return int(text)
    except ValueError:
        return service.store.resolve_claim_id(text)


def _add_cycle_policy_args(p: argparse.ArgumentParser, policy_default: str = "legacy") -> None:
    """Add shared --min-citations/--min-score/--policy-mode/--policy-limit args."""
    p.add_argument("--min-citations", type=int, default=1, help="Minimum citations to confirm candidate")
    p.add_argument("--min-score", type=float, default=0.58, help="Minimum score to confirm candidate")
    p.add_argument("--policy-mode", choices=list(POLICY_MODES), default=policy_default, help="Revalidation policy mode (legacy keeps candidate-only validation)")
    p.add_argument("--policy-limit", type=int, default=200, help="Max due claims selected for revalidation")


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
    qdrant_sync.add_argument("--qdrant-url", default="", help="Qdrant endpoint (default: $QDRANT_URL or 192.168.100.186:6333)")
    qdrant_sync.add_argument("--ollama-url", default="", help="Ollama endpoint (default: $OLLAMA_URL or 192.168.100.155:11434)")

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

    entity_cmd = sub.add_parser("extract-entities", help="Run entity extraction on claims via LLM")
    entity_cmd.add_argument("--limit", type=int, default=100, help="Max claims to process")
    entity_cmd.add_argument("--status", default="confirmed", help="Only process claims with this status")

    sub.add_parser("entity-stats", help="Show entity graph statistics")

    sub.add_parser("feedback-stats", help="Show feedback tracking and quality score statistics")

    sub.add_parser("quality-scores", help="Recompute quality scores for all claims")

    return parser


def print_claim(claim) -> None:
    hid = (getattr(claim, "human_id", None) or "")
    print(f"[{claim.id}]{f' {hid}' if hid else ''} {claim.status:<10} conf={claim.confidence:.3f} pin={int(claim.pinned)} "
          f"type={claim.claim_type or '-'} tuple=({claim.subject or '-'}, {claim.predicate or '-'}, {claim.object_value or '-'}) "
          f"scope={claim.scope} vol={claim.volatility} updated={claim.updated_at}\n  text: {claim.text}")
    if claim.supersedes_claim_id or claim.replaced_by_claim_id:
        print(f"  links: supersedes={claim.supersedes_claim_id or '-'} replaced_by={claim.replaced_by_claim_id or '-'}")
    for citation in claim.citations:
        print(f"  - cite: {citation.source}{f' | {citation.locator}' if citation.locator else ''}{f' | {citation.excerpt}' if citation.excerpt else ''}")


def _print_claim_brief(c) -> None:
    """Print a single-line claim summary used in ready/attention output."""
    hid = (getattr(c, "human_id", None) or "")
    print(f"  [{c.id}]{f' {hid}' if hid else ''} conf={c.confidence:.3f} scope={c.scope} {c.text[:80]}")


def _score_str_from_payload(payload_json: str | None) -> str:
    """Extract score from event payload_json for history display, or ''."""
    try:
        p = json.loads(payload_json) if payload_json else None
        return f"  score={p['score']}" if isinstance(p, dict) and "score" in p else ""
    except (json.JSONDecodeError, TypeError):
        return ""


def _event_to_timeline_entry(ev) -> dict:
    """Serialize an event into a timeline dict for history JSON output."""
    entry: dict = {"id": ev.id, "timestamp": ev.created_at, "event_type": ev.event_type}
    if ev.from_status or ev.to_status:
        entry.update({"from_status": ev.from_status, "to_status": ev.to_status})
    if ev.details:
        entry["details"] = ev.details
    if ev.payload_json:
        try:
            entry["payload"] = json.loads(ev.payload_json)
        except (json.JSONDecodeError, TypeError):
            entry["payload"] = ev.payload_json
    return entry


def _json_default(value):
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "dict"):
        return value.dict()
    if hasattr(value, "__dict__"):
        return value.__dict__
    return repr(value)


def _handle_snapshot_commands(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    """Handle snapshot, snapshots, rollback, diff, and install-hook subcommands."""
    db_resolved = Path(effective_db).resolve()

    if args.command == "snapshot":
        from memorymaster.snapshot import create_snapshot

        t0 = time.perf_counter()
        info = create_snapshot(db_resolved, workspace_root=Path(args.workspace).resolve(), message=args.message)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        if args.json_output:
            print(_json_envelope(asdict(info), query_ms=elapsed_ms))
        else:
            print(f"snapshot created: {info.snapshot_id}\n"
                  f"  commit: {info.commit_hash or '(no git)'}\n"
                  f"  time:   {info.timestamp}"
                  + (f"\n  msg:    {info.message}" if info.message else "")
                  + f"\n  size:   {info.size_bytes} bytes\n  path:   {info.path}")
        return 0

    if args.command == "snapshots":
        from memorymaster.snapshot import list_snapshots

        t0 = time.perf_counter()
        snaps = list_snapshots(db_resolved)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        items = [asdict(s) for s in snaps]
        if args.json_output:
            print(_json_envelope(items, total=len(items), query_ms=elapsed_ms))
        else:
            if not snaps:
                print("no snapshots found")
            else:
                for s in snaps:
                    print(f"  {s.snapshot_id}  {s.commit_hash[:8] if s.commit_hash else '(no git)'}  {s.timestamp}  {s.size_bytes}b{f'  {s.message}' if s.message else ''}")
                print(f"\n{len(snaps)} snapshot(s)")
        return 0

    if args.command == "rollback":
        from memorymaster.snapshot import rollback

        if not args.yes:
            try:
                answer = input(f"Restore DB from snapshot '{args.snapshot_id}'? A pre-rollback backup will be created. [y/N] ")
            except EOFError:
                answer = ""
            if answer.strip().lower() not in ("y", "yes"):
                print("rollback cancelled")
                return 0
        t0 = time.perf_counter()
        info = rollback(db_resolved, args.snapshot_id)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        if args.json_output:
            payload = {**asdict(info), "restored_snapshot_id": info.snapshot_id}
            print(_json_envelope(payload, query_ms=elapsed_ms))
        else:
            print(f"restored from snapshot: {info.snapshot_id}\n  commit: {info.commit_hash or '(no git)'}\n  time:   {info.timestamp}")
        return 0

    if args.command == "diff":
        from memorymaster.snapshot import diff_snapshot

        t0 = time.perf_counter()
        result = diff_snapshot(db_resolved, args.snapshot_id)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        if args.json_output:
            print(_json_envelope(asdict(result), query_ms=elapsed_ms))
        else:
            s = result.summary
            print(f"diff vs {result.snapshot_id}: +{s['added']} added, -{s['removed']} removed, "
                  f"~{s['changed']} changed, ={s['unchanged']} unchanged")
            for item in result.added:
                print(f"  + [{item['id']}] {item['status']}: {item['text'][:80]}")
            for item in result.removed:
                print(f"  - [{item['id']}] {item['status']}: {item['text'][:80]}")
            for item in result.changed:
                changes = ", ".join(f"{k}: {v['old']!r}->{v['new']!r}" for k, v in item["changes"].items())
                print(f"  ~ [{item['id']}] {changes}")
        return 0

    if args.command == "install-hook":
        from memorymaster.snapshot import install_git_hook

        t0 = time.perf_counter()
        result = install_git_hook(Path(args.workspace).resolve())
        elapsed_ms = (time.perf_counter() - t0) * 1000
        if args.json_output:
            print(_json_envelope(result, query_ms=elapsed_ms))
        else:
            if result["installed"]:
                print(f"post-commit hook {'appended to existing' if result.get('appended') else 'created'}: {result['path']}")
            else:
                print(f"hook not installed: {result.get('reason', 'unknown')}")
        return 0


def _handle_qdrant_commands(args: argparse.Namespace, service, parser: argparse.ArgumentParser) -> int:
    """Handle qdrant-sync and qdrant-search subcommands."""
    from memorymaster.qdrant_backend import QdrantBackend

    qdrant_url = args.qdrant_url or os.environ.get("QDRANT_URL") or ""
    ollama_url = args.ollama_url or os.environ.get("OLLAMA_URL") or ""
    qdrant_kw = {k: v for k, v in [("qdrant_url", qdrant_url), ("ollama_url", ollama_url)] if v}
    backend = QdrantBackend(**qdrant_kw)
    t0 = time.perf_counter()

    if args.command == "qdrant-sync":
        result = backend.sync_all(service.store)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        if args.json_output:
            print(_json_envelope(result, query_ms=elapsed_ms))
        else:
            print(f"Qdrant sync: {result['synced']}/{result['total']} synced, {result['errors']} errors ({elapsed_ms:.0f}ms)")
        return 0

    # qdrant-search
    states = [s.strip() for s in args.states.split(",") if s.strip()] or None
    results = backend.search(args.text, limit=args.limit, min_confidence=args.min_confidence, states=states)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if args.json_output:
        print(_json_envelope({"results": results, "count": len(results)}, query_ms=elapsed_ms))
    else:
        if not results:
            print("No results found.")
        for hit in results:
            _pl = hit.get("payload", {})
            print(f"[{hit.get('claim_id', '?')}] score={hit.get('score', 0.0):.3f} "
                  f"state={_pl.get('state', '?')} conf={_pl.get('confidence', 0.0):.2f} {_pl.get('claim_text', '')[:100]}")
    return 0


def _handle_link_commands(args: argparse.Namespace, service, parser: argparse.ArgumentParser) -> int:
    """Handle link, unlink, and links subcommands."""
    if args.command == "link":
        t0 = time.perf_counter()
        link = service.add_claim_link(_resolve_claim_id(service, args.source_id), _resolve_claim_id(service, args.target_id), args.link_type)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        if args.json_output:
            print(_json_envelope(asdict(link), query_ms=elapsed_ms))
        else:
            print(f"linked claim {link.source_id} -> {link.target_id} ({link.link_type}) id={link.id}")
        return 0

    if args.command == "unlink":
        t0 = time.perf_counter()
        src_id = _resolve_claim_id(service, args.source_id)
        tgt_id = _resolve_claim_id(service, args.target_id)
        removed = service.remove_claim_link(src_id, tgt_id, args.link_type)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        if args.json_output:
            print(_json_envelope({"removed": removed, "source_id": src_id, "target_id": tgt_id}, query_ms=elapsed_ms))
        else:
            print(f"removed {removed} link(s) between {src_id} and {tgt_id}")
        return 0

    if args.command == "links":
        t0 = time.perf_counter()
        resolved_id = _resolve_claim_id(service, args.claim_id)
        links = service.get_claim_links(resolved_id)
        if args.link_type:
            links = [lnk for lnk in links if lnk.link_type == args.link_type]
        elapsed_ms = (time.perf_counter() - t0) * 1000
        items = [asdict(lnk) for lnk in links]
        if args.json_output:
            print(_json_envelope({"rows": len(items), "links": items}, total=len(items), query_ms=elapsed_ms))
        else:
            print(json.dumps({"rows": len(items), "links": items}, indent=2))
        return 0


def _resolve_db_path(args: argparse.Namespace) -> str:
    """Resolve effective DB path; activates stealth if --stealth or stealth DB exists in cwd."""
    stealth_path = Path.cwd() / STEALTH_DB_NAME
    if args.stealth or (args.db == "memorymaster.db" and stealth_path.exists()):
        return str(stealth_path)
    return args.db


def _stealth_active(args: argparse.Namespace) -> bool:
    """Return True if stealth mode is active for the resolved args."""
    return _resolve_db_path(args) != args.db or args.stealth


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    effective_db = _resolve_db_path(args)

    try:
        if args.command == "stealth-status":
            t0 = time.perf_counter()
            stealth_path = Path.cwd() / STEALTH_DB_NAME
            active = _stealth_active(args)
            db_display = str(Path(effective_db).resolve()) if "://" not in effective_db else effective_db
            elapsed_ms = (time.perf_counter() - t0) * 1000
            info = {"stealth_active": active, "stealth_db_exists": stealth_path.exists(),
                    "stealth_db_path": str(stealth_path.resolve()), "effective_db": db_display,
                    "gitignore_hint": f"Add '{STEALTH_DB_NAME}' to your .gitignore"}
            if args.json_output:
                print(_json_envelope(info, query_ms=elapsed_ms))
            else:
                print(f"stealth mode: {'ACTIVE' if active else 'inactive'}\n"
                      f"stealth db exists: {stealth_path.exists()}\n"
                      f"stealth db path: {stealth_path.resolve()}\n"
                      f"effective db: {db_display}")
                if not active:
                    print("\nTip: run 'memorymaster --stealth init-db' to create a stealth DB here.")
                print(f"\nRemember to add '{STEALTH_DB_NAME}' to your .gitignore")
            return 0

        if args.command == "export-metrics":
            from memorymaster.metrics_exporter import export_metrics

            snapshot = export_metrics(events_jsonl=[Path(p) for p in args.events_jsonl],
                out_prom=Path(args.out_prom), out_json=Path(args.out_json))
            c = snapshot.get("counters", {})
            print(json.dumps({"command": "export-metrics",
                "events_total": int(c.get("events_total", 0)), "transitions_total": int(c.get("transitions_total", 0)),
                "status_total": int(c.get("status_total", 0)),
                "out_prom": args.out_prom, "out_json": args.out_json}, indent=2))
            return 0

        service = MemoryService(effective_db, workspace_root=Path(args.workspace),
            tenant_id=getattr(args, "tenant", None))

        if args.command == "init-db":
            t0 = time.perf_counter()
            service.init_db()
            elapsed_ms = (time.perf_counter() - t0) * 1000
            db_path = effective_db if "://" in effective_db else str(Path(effective_db).resolve())
            if args.json_output:
                print(_json_envelope({"db": db_path, "stealth": _stealth_active(args)}, query_ms=elapsed_ms))
            else:
                print(f"initialized db: {db_path}{' (stealth)' if _stealth_active(args) else ''}")
            return 0

        if args.command == "ingest":
            citations = [parse_citation(raw) for raw in args.source]
            t0 = time.perf_counter()
            claim = service.ingest(
                text=args.text, citations=citations, idempotency_key=args.idempotency_key,
                claim_type=args.claim_type, subject=args.subject, predicate=args.predicate,
                object_value=args.object_value, scope=args.scope, volatility=args.volatility, confidence=args.confidence,
                event_time=args.event_time, valid_from=args.valid_from, valid_until=args.valid_until,
            )
            elapsed_ms = (time.perf_counter() - t0) * 1000
            if args.json_output:
                print(_json_envelope(_claim_to_dict(claim), total=1, query_ms=elapsed_ms))
            else:
                hid = getattr(claim, "human_id", None) or ""
                print(f"ingested claim_id={claim.id}{f' human_id={hid}' if hid else ''} status={claim.status} citations={len(claim.citations)}")
            return 0

        if args.command == "run-cycle":
            t0 = time.perf_counter()
            result = service.run_cycle(run_compactor=args.with_compact, min_citations=args.min_citations,
                min_score=args.min_score, policy_mode=args.policy_mode, policy_limit=args.policy_limit)
            elapsed_ms = (time.perf_counter() - t0) * 1000
            if args.json_output:
                print(_json_envelope(result, query_ms=elapsed_ms))
            else:
                print(json.dumps(result, indent=2))
            return 0

        if args.command == "query":
            resolve_allow_sensitive_access(allow_sensitive=args.allow_sensitive, context="cli.query")
            t0 = time.perf_counter()
            rows_data = service.query_rows(
                query_text=args.text, limit=args.limit,
                include_stale=not args.exclude_stale, include_conflicted=not args.exclude_conflicted,
                include_candidates=getattr(args, "include_candidates", False),
                retrieval_mode=args.retrieval_mode, allow_sensitive=args.allow_sensitive,
                scope_allowlist=parse_scope_allowlist(args.scope_allowlist),
            )
            elapsed_ms = (time.perf_counter() - t0) * 1000
            if args.json_output:
                json_rows = [{"claim": _claim_to_dict(row["claim"]),
                    **{k: float(row.get(k, 0.0)) for k in _SCORE_KEYS},
                    "annotation": row.get("annotation", {})} for row in rows_data]
                print(_json_envelope(json_rows, total=len(json_rows), query_ms=elapsed_ms))
            else:
                for row in rows_data:
                    print_claim(row["claim"])
                    ann = row.get("annotation", {})
                    sc = {k: float(row.get(k, 0.0)) for k in _SCORE_KEYS}
                    print(f"  retrieval: score={sc['score']:.3f} lex={sc['lexical_score']:.3f} "
                          f"conf={sc['confidence_score']:.3f} fresh={sc['freshness_score']:.3f} "
                          f"vec={sc['vector_score']:.3f} "
                          f"active={int(bool(ann.get('active')))} stale={int(bool(ann.get('stale')))} "
                          f"conflicted={int(bool(ann.get('conflicted')))} pinned={int(bool(ann.get('pinned')))}")
                print(f"rows={len(rows_data)}")
            return 0

        if args.command == "context":
            resolve_allow_sensitive_access(allow_sensitive=args.allow_sensitive, context="cli.context")
            t0 = time.perf_counter()
            result = service.query_for_context(
                query=args.text, token_budget=args.budget, output_format=args.output_format,
                limit=args.limit, include_stale=not args.exclude_stale,
                include_conflicted=not args.exclude_conflicted,
                include_candidates=getattr(args, "include_candidates", False),
                retrieval_mode=args.retrieval_mode, allow_sensitive=args.allow_sensitive,
                scope_allowlist=parse_scope_allowlist(args.scope_allowlist),
            )
            elapsed_ms = (time.perf_counter() - t0) * 1000
            if args.json_output:
                print(_json_envelope(asdict(result), query_ms=elapsed_ms))
            else:
                print(result.output)
            return 0

        if args.command == "pin":
            claim = service.pin(_resolve_claim_id(service, args.claim_id), pin=not args.unpin)
            print(f"claim_id={claim.id} status={claim.status} pinned={int(claim.pinned)}")
            return 0

        if args.command == "redact-claim":
            result = service.redact_claim_payload(_resolve_claim_id(service, args.claim_id), mode=args.mode,
                redact_claim=not args.citations_only, redact_citations=not args.claims_only,
                reason=(args.reason.strip() or None), actor=args.actor)
            print(json.dumps(result, indent=2, default=_json_default))
            return 0

        if args.command == "compact":
            result = service.compact(retain_days=args.retain_days, event_retain_days=args.event_retain_days)
            print(json.dumps(result, indent=2))
            return 0

        if args.command == "compact-summaries":
            from memorymaster.llm_steward import _parse_api_keys
            rk = _parse_api_keys(api_key=args.api_key, api_keys=args.api_keys)
            t0 = time.perf_counter()
            result = service.compact_summaries(
                provider=args.provider, api_key=rk[0] if len(rk) == 1 else "",
                model=args.model, base_url=args.base_url, min_cluster=args.min_cluster,
                max_cluster=args.max_cluster, similarity_threshold=args.similarity_threshold,
                dry_run=args.dry_run, limit=args.limit,
                api_keys=rk if len(rk) > 1 else None, cooldown_seconds=args.cooldown,
            )
            elapsed_ms = (time.perf_counter() - t0) * 1000
            if args.json_output:
                print(_json_envelope(result, query_ms=elapsed_ms))
            else:
                print(f"compact-summaries [{'DRY RUN' if result['dry_run'] else 'APPLIED'}] clusters={result['clusters_found']} "
                      f"summaries={result['summaries_created']} source_claims={result['source_claims_summarized']} errors={result['errors']}")
                for d in result.get("details", []):
                    _a, _ids, _sub = d.get("action", "unknown"), d.get("source_claim_ids", []), d.get("subject_hint", "")
                    if _a == "summarized":
                        print(f"  [{_a}] summary_id={d.get('summary_claim_id')} from {len(_ids)} claims (subject: {_sub})\n    {d.get('summary_text', '')[:120]}")
                    elif _a == "would_summarize":
                        print(f"  [{_a}] {len(_ids)} claims (subject: {_sub})")
                    else:
                        print(f"  [{_a}] {len(_ids)} claims (subject: {_sub}) {d.get('error', '')[:80]}")
            return 0

        if args.command == "dedup":
            t0 = time.perf_counter()
            result = service.dedup(threshold=args.threshold, min_text_overlap=args.min_text_overlap, dry_run=args.dry_run)
            elapsed_ms = (time.perf_counter() - t0) * 1000
            if args.json_output:
                print(_json_envelope(result, query_ms=elapsed_ms))
            else:
                print(f"dedup [{'DRY RUN' if result['dry_run'] else 'APPLIED'}] scanned={result['scanned']} "
                      f"duplicates={result['duplicates_found']} archived={result['claims_archived']} threshold={result['threshold']}")
                for pair in result["pairs"]:
                    print(f"  dup: keep={pair['keep_id']} archive={pair['archive_id']} sim={pair['similarity']:.4f} overlap={pair['text_overlap']:.4f}\n"
                          f"    keep:    {pair['keep_text'][:80]}\n    archive: {pair['archive_text'][:80]}")
            return 0

        if args.command == "recompute-tiers":
            t0 = time.perf_counter()
            counts = service.recompute_tiers()
            elapsed_ms = (time.perf_counter() - t0) * 1000
            if args.json_output:
                print(_json_envelope(counts, query_ms=elapsed_ms))
            else:
                print(f"recompute-tiers: core={counts['core']} working={counts['working']} peripheral={counts['peripheral']}")
            return 0

        if args.command == "list-claims":
            resolve_allow_sensitive_access(allow_sensitive=args.allow_sensitive, context="cli.list-claims")
            t0 = time.perf_counter()
            claims = service.list_claims(status=args.status, limit=args.limit,
                include_archived=args.include_archived, allow_sensitive=args.allow_sensitive)
            elapsed_ms = (time.perf_counter() - t0) * 1000
            if args.json_output:
                print(_json_envelope([_claim_to_dict(c) for c in claims], total=len(claims), query_ms=elapsed_ms))
            else:
                for claim in claims:
                    print_claim(claim)
                print(f"rows={len(claims)}")
            return 0

        if args.command == "list-events":
            t0 = time.perf_counter()
            events = service.list_events(claim_id=args.claim_id, limit=args.limit, event_type=args.event_type)
            elapsed_ms = (time.perf_counter() - t0) * 1000
            if args.json_output:
                print(_json_envelope(json.loads(json.dumps(events, default=_json_default)), total=len(events), query_ms=elapsed_ms))
            else:
                print(json.dumps({"rows": len(events), "events": events}, indent=2, default=_json_default))
            return 0

        if args.command == "history":
            t0 = time.perf_counter()
            resolved_id = _resolve_claim_id(service, args.claim_id)
            claim = service.store.get_claim(resolved_id, include_citations=False)
            if claim is None:
                if args.json_output:
                    print(_json_error(f"Claim {args.claim_id} not found."))
                else:
                    print(f"Error: Claim {args.claim_id} not found.")
                return 1
            events = service.list_events(claim_id=resolved_id, limit=args.limit)
            events.reverse()  # list_events returns newest-first
            elapsed_ms = (time.perf_counter() - t0) * 1000

            if args.json_output:
                print(_json_envelope({"claim_id": args.claim_id, "status": claim.status,
                    "confidence": claim.confidence, "timeline": [_event_to_timeline_entry(ev) for ev in events]},
                    total=len(events), query_ms=elapsed_ms))
            else:
                print(f"=== History for claim {claim.id} [{claim.status} conf={claim.confidence:.3f}] ===\n  text: {claim.text}\n")
                for ev in events:
                    transition = f"  {ev.from_status or '?'} -> {ev.to_status or '?'}" if (ev.from_status or ev.to_status) else ""
                    details_str = f"  | {ev.details}" if ev.details else ""
                    print(f"  {ev.created_at}  {ev.event_type:<25}{transition}{_score_str_from_payload(ev.payload_json)}{details_str}")
                print(f"\n  ({len(events)} events)")
            return 0

        if args.command == "review-queue":
            from memorymaster.review import build_review_queue, queue_to_dicts

            items = build_review_queue(service, limit=args.limit,
                include_stale=not args.exclude_stale, include_conflicted=not args.exclude_conflicted,
                include_sensitive=resolve_allow_sensitive_access(allow_sensitive=args.allow_sensitive, context="cli.review-queue"))
            print(json.dumps({"rows": len(items), "items": queue_to_dicts(items)}, indent=2))
            return 0

        if args.command == "run-daemon":
            result = run_daemon(service,
                interval_seconds=args.interval_seconds, max_cycles=args.max_cycles,
                compact_every=args.compact_every, min_citations=args.min_citations,
                min_score=args.min_score, policy_mode=args.policy_mode, policy_limit=args.policy_limit,
                git_trigger=args.git_trigger, git_check_seconds=args.git_check_seconds)
            print(json.dumps(result, indent=2))
            return 0

        if args.command == "run-dashboard":
            from memorymaster.dashboard import create_dashboard_server

            server = create_dashboard_server(db_target=effective_db, workspace_root=args.workspace,
                host=args.host, port=args.port, operator_log_jsonl=args.operator_log_jsonl)
            print(f"memorymaster dashboard listening on http://{args.host}:{args.port}/dashboard")
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
            return 0

        if args.command == "run-operator":
            try:
                from memorymaster.operator import MemoryOperator, OperatorConfig
            except Exception as exc:
                print(f"error: run-operator unavailable: could not import memorymaster.operator ({exc})")
                return 2

            def _stateful(val: str) -> str | None:
                return None if args.no_state else (val.strip() or None)

            config = OperatorConfig(
                reconcile_interval_seconds=args.reconcile_seconds,
                retrieval_mode=args.retrieval_mode, retrieval_limit=args.query_limit,
                progressive_retrieval=not args.disable_progressive_retrieval,
                tier1_limit=args.tier1_limit, tier2_limit=args.tier2_limit,
                min_citations=args.min_citations, min_score=args.min_score,
                policy_mode=args.policy_mode, policy_limit=args.policy_limit, compact_every=args.compact_every,
                max_idle_seconds=args.max_idle_seconds if args.max_idle_seconds and args.max_idle_seconds > 0 else None,
                log_jsonl_path=(args.log_jsonl.strip() or None),
                state_json_path=_stateful(args.state_json), queue_state_json_path=_stateful(args.queue_state_json),
                queue_journal_jsonl_path=_stateful(args.queue_journal_jsonl), queue_db_path=_stateful(args.queue_db),
            )
            operator = MemoryOperator(service=service, config=config)
            result = operator.run_stream(inbox_jsonl=Path(args.inbox_jsonl),
                poll_seconds=args.poll_seconds, max_events=args.max_events)
            print(json.dumps(result, indent=2, default=_json_default))
            return 0

        if args.command == "run-steward":
            from memorymaster.steward import run_steward

            t0 = time.perf_counter()
            result = run_steward(
                service, mode=args.mode, cadence_trigger=args.cadence_trigger,
                interval_seconds=args.interval_seconds, git_check_seconds=args.git_check_seconds,
                commit_every=args.commit_every, max_cycles=args.max_cycles,
                allow_sensitive=resolve_allow_sensitive_access(allow_sensitive=args.allow_sensitive, context="cli.run-steward"),
                apply=args.apply, max_claims=args.max_claims, max_proposals=args.max_proposals,
                max_probe_files=args.max_probe_files, max_probe_file_bytes=args.max_probe_file_bytes,
                max_tool_probes=args.max_tool_probes, probe_timeout_seconds=args.probe_timeout_seconds,
                probe_failure_threshold=args.probe_failure_threshold,
                enable_semantic_probe=not args.disable_semantic_probe,
                enable_tool_probe=not args.disable_tool_probe,
                artifact_path=Path(args.artifact_json),
            )
            elapsed_ms = (time.perf_counter() - t0) * 1000
            if args.json_output:
                print(_json_envelope(json.loads(json.dumps(result, default=_json_default)), query_ms=elapsed_ms))
            else:
                print(json.dumps(result, indent=2, default=_json_default))
            return 0

        if args.command == "steward-proposals":
            from memorymaster.steward import list_steward_proposals

            rows = list_steward_proposals(service, limit=args.limit, include_resolved=args.include_resolved)
            print(json.dumps({"rows": len(rows), "proposals": rows}, indent=2, default=_json_default))
            return 0

        if args.command == "resolve-proposal":
            if args.proposal_event_id is None and args.claim_id is None:
                raise ValueError("resolve-proposal requires --proposal-event-id or --claim-id")
            from memorymaster.steward import resolve_steward_proposal

            result = resolve_steward_proposal(service, action=args.action,
                proposal_event_id=args.proposal_event_id, claim_id=args.claim_id, apply_on_approve=not args.no_apply)
            print(json.dumps(result, indent=2, default=_json_default))
            return 0

        if args.command in ("link", "unlink", "links"):
            return _handle_link_commands(args, service, parser)

        if args.command == "ready":
            from memorymaster.conflict_resolver import detect_conflicts

            t0 = time.perf_counter()
            limit = args.limit

            stale_claims = service.store.list_claims(status="stale", limit=limit, include_archived=False, include_citations=True)
            conflict_pairs = detect_conflicts(service.store, limit=500)
            all_candidates = service.store.list_claims(status="candidate", limit=limit * 3, include_archived=False)
            threshold = args.confidence_threshold
            low_conf_candidates = [c for c in all_candidates if c.confidence < threshold][:limit]
            elapsed_ms = (time.perf_counter() - t0) * 1000

            payload = {
                "stale": {"count": len(stale_claims), "claims": [_claim_to_dict(c) for c in stale_claims]},
                "conflicted": {"count": len(conflict_pairs), "pairs": [
                    {"winner_id": p.winner.id, "loser_id": p.loser.id, "reason": p.reason,
                     "key": list(p.key), "winner_text": p.winner.text[:120],
                     "loser_text": p.loser.text[:120], "winner_confidence": p.winner.confidence,
                     "loser_confidence": p.loser.confidence}
                    for p in conflict_pairs[:limit]
                ]},
                "low_confidence": {"count": len(low_conf_candidates), "claims": [_claim_to_dict(c) for c in low_conf_candidates]},
                "total_attention": len(stale_claims) + len(conflict_pairs) + len(low_conf_candidates),
            }

            if args.json_output:
                print(_json_envelope(payload, total=payload["total_attention"], query_ms=elapsed_ms))
            else:
                total = payload["total_attention"]
                if total == 0:
                    print("Nothing needs attention. All clear.")
                    return 0

                print(f"=== {total} items need attention ===\n")

                if stale_claims:
                    print(f"--- Stale claims ({len(stale_claims)}) ---\n  Previously confirmed but source files changed. Need re-validation.")
                    for c in stale_claims:
                        _print_claim_brief(c)
                    print('  -> Run `memorymaster check-staleness` to review details\n')

                if conflict_pairs:
                    print(f"--- Conflicted pairs ({len(conflict_pairs)}) ---\n  Same subject+predicate with different values. Need resolution.")
                    for p in conflict_pairs[:limit]:
                        print(f"  winner=[{p.winner.id}] vs loser=[{p.loser.id}] "
                              f"key=({p.key[0]}, {p.key[1]}) reason={p.reason}")
                    print('  -> Run `memorymaster resolve-conflicts` to auto-resolve\n')

                if low_conf_candidates:
                    print(f"--- Low-confidence candidates ({len(low_conf_candidates)}) ---\n  Candidates with confidence < {threshold}. Need more evidence or review.")
                    for c in low_conf_candidates:
                        _print_claim_brief(c)
                    print('  -> Run `memorymaster run-cycle` to re-evaluate candidates\n')

            return 0

        if args.command == "resolve-conflicts":
            from memorymaster.conflict_resolver import resolve_conflicts

            t0 = time.perf_counter()
            result = resolve_conflicts(service, dry_run=args.dry_run, limit=args.limit)
            elapsed_ms = (time.perf_counter() - t0) * 1000
            if args.json_output:
                print(_json_envelope(asdict(result), query_ms=elapsed_ms))
            else:
                if args.dry_run:
                    print("[DRY RUN] No transitions applied.")
                print(f"conflicts detected={result.pairs_detected} resolved={result.pairs_resolved} skipped={result.pairs_skipped}")
                for res in result.resolutions:
                    status = "APPLIED" if res.get("applied") else "SKIPPED"
                    skip = f" ({res['skip_reason']})" if res.get("skip_reason") else ""
                    print(f"  [{status}] winner={res['winner_id']} loser={res['loser_id']} reason={res['reason']}{skip}")
            return 0

        if args.command == "check-staleness":
            from memorymaster.jobs.staleness import run as run_staleness

            t0 = time.perf_counter()
            result = run_staleness(service.store, Path(args.workspace).resolve(), mode=args.mode, dry_run=args.dry_run, limit=args.limit)
            elapsed_ms = (time.perf_counter() - t0) * 1000
            if args.json_output:
                print(_json_envelope(asdict(result), query_ms=elapsed_ms))
            else:
                if args.dry_run:
                    print("[DRY RUN] No transitions applied.")
                print(f"staleness scanned={result.scanned} stale={result.stale_detected} "
                      f"already_stale={result.already_stale} skipped_pinned={result.skipped_pinned} "
                      f"skipped_no_citations={result.skipped_no_citations}")
                for d in result.details:
                    print(f"  [{'APPLIED' if d.get('applied') else 'DETECTED'}] claim={d['claim_id']} "
                          f"files={', '.join(os.path.basename(f) for f in d.get('changed_files', [])[:3])}")
            return 0

        if args.command in ("snapshot", "snapshots", "rollback", "diff", "install-hook"):
            return _handle_snapshot_commands(args, service, parser, effective_db)

        if args.command in ("qdrant-sync", "qdrant-search"):
            return _handle_qdrant_commands(args, service, parser)

        if args.command == "export-vault":
            from memorymaster.vault_exporter import export_vault
            t0 = time.perf_counter()
            result = export_vault(service.store, output_dir=args.output, scope_filter=args.scope or None, confirmed_only=args.confirmed_only, include_archived=args.include_archived)
            elapsed_ms = (time.perf_counter() - t0) * 1000
            if args.json_output:
                print(_json_envelope(result, query_ms=elapsed_ms))
            else:
                print(f"Exported {result['exported']} claims to {args.output} ({result['directories_created']} dirs, {elapsed_ms:.0f}ms)")
            return 0

        if args.command == "extract-entities":
            from memorymaster.entity_graph import EntityGraph
            t0 = time.perf_counter()
            eg = EntityGraph(str(effective_db))
            eg.ensure_tables()
            claims = service.store.find_by_status(args.status, limit=args.limit, include_citations=False)
            total_entities = 0
            for claim in claims:
                names = eg.extract_and_link(claim.id, claim.text)
                total_entities += len(names)
                if names:
                    print(f"  [{claim.id}] {', '.join(names)}")
            elapsed_ms = (time.perf_counter() - t0) * 1000
            stats = eg.get_stats()
            if args.json_output:
                print(_json_envelope({"extracted": total_entities, "claims_processed": len(claims), **stats}, query_ms=elapsed_ms))
            else:
                print(f"Extracted {total_entities} entities from {len(claims)} claims ({elapsed_ms:.0f}ms)")
                print(f"Graph: {stats['entities']} entities, {stats['edges']} edges, {stats['claim_links']} links")
            return 0

        if args.command == "entity-stats":
            from memorymaster.entity_graph import EntityGraph
            eg = EntityGraph(str(effective_db))
            eg.ensure_tables()
            stats = eg.get_stats()
            if args.json_output:
                print(_json_envelope(stats))
            else:
                print(f"Entities: {stats['entities']}, Edges: {stats['edges']}, Claim links: {stats['claim_links']}")
                for t, c in stats.get('by_type', {}).items():
                    print(f"  {t}: {c}")
            return 0

        if args.command == "feedback-stats":
            from memorymaster.feedback import FeedbackTracker
            ft = FeedbackTracker(str(effective_db))
            ft.ensure_tables()
            stats = ft.get_stats()
            if args.json_output:
                print(_json_envelope(stats))
            else:
                print(f"Feedback rows: {stats['feedback_rows']}, Claims scored: {stats['claims_scored']}, Avg quality: {stats['avg_quality']}")
            return 0

        if args.command == "quality-scores":
            from memorymaster.feedback import FeedbackTracker
            t0 = time.perf_counter()
            ft = FeedbackTracker(str(effective_db))
            ft.ensure_tables()
            result = ft.compute_quality_scores()
            elapsed_ms = (time.perf_counter() - t0) * 1000
            if args.json_output:
                print(_json_envelope(result, query_ms=elapsed_ms))
            else:
                print(f"Quality scores computed for {result['scored']} claims ({elapsed_ms:.0f}ms)")
            return 0

        parser.print_help()
        return 1
    except Exception as exc:
        if args.json_output:
            print(_json_error(str(exc)))
        else:
            print(f"error: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

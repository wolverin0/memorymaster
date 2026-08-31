"""Deterministic trajectory metrics and outcome/verification inference."""

from __future__ import annotations

import re
from collections import Counter
from typing import Iterable

from .models import ActionRecord, FeedbackRecord, SessionRecord, TurnRecord


VERIFICATION_TIERS = (
    "none", "syntax_static", "unit", "integration_build", "runtime_api",
    "browser_visual", "deployed_identity", "natural_external_acceptance",
)
_TIER_RANK = {name: index for index, name in enumerate(VERIFICATION_TIERS)}
_COMPLETION = re.compile(r"(?i)\b(done|complete(?:d)?|fixed|implemented|working|resolved)\b")
_DEPLOYED = re.compile(r"(?i)\b(deployed|in production|live)\b")
_BLOCKED = re.compile(r"(?i)\b(blocked|cannot continue|need(?:s)? (?:access|approval))\b")
_VERIFICATION_MAP: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)(natural[-_ ]run|external acceptance|recipient ack)"), "natural_external_acceptance"),
    (re.compile(r"(?i)(deployed.identity|production.identity|release.identity)"), "deployed_identity"),
    (re.compile(r"(?i)(playwright|cypress|browser|visual|screenshot)"), "browser_visual"),
    (re.compile(r"(?i)(curl|http|api|runtime|smoke)"), "runtime_api"),
    (re.compile(r"(?i)(build|integration|e2e)"), "integration_build"),
    (re.compile(r"(?i)(pytest|unittest|jest|vitest|cargo test|go test|test)"), "unit"),
    (re.compile(r"(?i)(ruff|mypy|typecheck|tsc|compile|syntax)"), "syntax_static"),
)


def verification_tier(actions: Iterable[ActionRecord]) -> str:
    best = "none"
    for action in actions:
        if action.kind != "verification" or action.status != "success":
            continue
        text = f"{action.name} {action.command_family}"
        tier = next((name for pattern, name in _VERIFICATION_MAP if pattern.search(text)), "syntax_static")
        if _TIER_RANK[tier] > _TIER_RANK[best]:
            best = tier
    return best


def retry_loop_count(actions: Iterable[ActionRecord]) -> int:
    loops = 0
    previous = ""
    failed_run = 0
    for action in actions:
        family = action.command_family or action.name.lower()
        if action.status == "failed" and family == previous:
            failed_run += 1
        elif action.status == "failed":
            previous, failed_run = family, 1
        else:
            previous, failed_run = "", 0
        if failed_run == 2:
            loops += 1
    return loops


def _completion_state(
    turns: list[TurnRecord], actions: list[ActionRecord], tier: str,
) -> tuple[str, bool]:
    assistant_text = " ".join(turn.excerpt for turn in turns if turn.role == "assistant")
    completion_claimed = bool(_COMPLETION.search(assistant_text))
    mutation = any(action.kind == "mutation" for action in actions)
    if _BLOCKED.search(assistant_text):
        return "blocked", completion_claimed
    if _DEPLOYED.search(assistant_text) and _TIER_RANK[tier] >= _TIER_RANK["deployed_identity"]:
        return "deployed", completion_claimed
    if tier == "natural_external_acceptance":
        return "externally_accepted", completion_claimed
    if _TIER_RANK[tier] >= _TIER_RANK["runtime_api"]:
        return "runtime_verified", completion_claimed
    if tier != "none":
        return "locally_verified", completion_claimed
    if mutation:
        return "implemented", completion_claimed
    return "unknown", completion_claimed


def analyze_session(
    session: SessionRecord,
    turns: list[TurnRecord],
    actions: list[ActionRecord],
    feedback: list[FeedbackRecord],
) -> dict[str, object]:
    first_mutation = next((index for index, item in enumerate(actions) if item.kind == "mutation"), None)
    research_before = sum(
        item.kind == "read" for index, item in enumerate(actions)
        if first_mutation is not None and index < first_mutation
    )
    tier = verification_tier(actions)
    state, completion_claimed = _completion_state(turns, actions, tier)
    flags: list[str] = []
    mutation_before_research = first_mutation is not None and research_before == 0
    if mutation_before_research:
        flags.append("mutation_before_research")
    retries = retry_loop_count(actions)
    if retries:
        flags.append("retry_loop")
    if completion_claimed and first_mutation is not None and tier == "none":
        flags.append("completion_without_verification")
    themes = Counter(item.theme for item in feedback if item.user_origin)
    return {
        "session_id": session.session_id,
        "mutation_before_research": mutation_before_research,
        "reads_before_first_mutation": research_before,
        "retry_loops": retries,
        "completion_claimed": completion_claimed,
        "completion_state": state,
        "verification_tier": tier,
        "correction_count": sum(themes.values()),
        "correction_themes": dict(sorted(themes.items())),
        "action_counts": dict(sorted(Counter(item.kind for item in actions).items())),
        "flags": flags,
    }


__all__ = ["VERIFICATION_TIERS", "analyze_session", "retry_loop_count", "verification_tier"]

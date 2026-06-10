"""Rule-shaped claims (v3.21.0-R1).

Borrows the prescriptive shape from ReflexioAI/claude-smart without
installing it. MemoryMaster's normal claims are *descriptive* ("the API
uses Y"); a rule is *prescriptive* — "when <trigger>, do <action> because
<rationale>" — the shape Claude needs to actually change behaviour next
time.

Storage decision: a rule is an ordinary claim with
``claim_type == "rule"`` and its structured fields packed as JSON in
``object_value``. This needs NO schema change and NO migration. It is
safe because MemoryMaster's deterministic value-validators are
*predicate-gated* (they only inspect ``object_value`` when the predicate
indicates an IP / URL / date / etc.), and rule claims use
``predicate == "applies_when"`` which matches none of those — so the JSON
payload is never misread as a typed value.

Field mapping:
    claim_type    = "rule"
    predicate     = "applies_when"        (keeps deterministic validators out)
    subject       = trigger               (short, for lexical/vector match)
    object_value  = JSON {trigger, action, rationale}
    text          = human-readable "When <trigger>, <action>. (<rationale>)"
"""
from __future__ import annotations

import json
from typing import Any

RULE_CLAIM_TYPE = "rule"
RULE_PREDICATE = "applies_when"


def render_rule_text(trigger: str, action: str, rationale: str) -> str:
    """Human-readable single-line form of a rule (used as claim.text)."""
    base = f"When {trigger.strip()}, {action.strip()}."
    rationale = rationale.strip()
    if rationale:
        base += f" ({rationale})"
    return base


def build_rule_fields(trigger: str, action: str, rationale: str = "") -> dict[str, Any]:
    """Return kwargs for ``MemoryService.ingest`` that store a rule-shaped claim.

    Caller still supplies citations / scope / source_agent as usual::

        svc.ingest(**build_rule_fields(trigger, action, rationale),
                   citations=[...], source_agent="...")
    """
    trigger = (trigger or "").strip()
    action = (action or "").strip()
    if not trigger or not action:
        raise ValueError("a rule needs both a trigger and an action")

    payload = json.dumps(
        {"trigger": trigger, "action": action, "rationale": (rationale or "").strip()},
        ensure_ascii=False,
        sort_keys=True,
    )
    return {
        "text": render_rule_text(trigger, action, rationale),
        "claim_type": RULE_CLAIM_TYPE,
        "subject": trigger,
        "predicate": RULE_PREDICATE,
        "object_value": payload,
    }


def is_rule(claim: Any) -> bool:
    return getattr(claim, "claim_type", None) == RULE_CLAIM_TYPE


def parse_rule(claim: Any) -> dict[str, Any] | None:
    """Extract a rule's structured fields from a rule-typed claim.

    Returns ``{trigger, action, rationale, text, claim_id}`` or ``None`` if
    the claim is not a rule. Tolerates a malformed/empty ``object_value`` by
    falling back to the claim's subject/text.
    """
    if not is_rule(claim):
        return None

    trigger = getattr(claim, "subject", None) or ""
    action = ""
    rationale = ""
    raw = getattr(claim, "object_value", None)
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                trigger = data.get("trigger", trigger) or trigger
                action = data.get("action", "") or ""
                rationale = data.get("rationale", "") or ""
        except (json.JSONDecodeError, TypeError):
            pass

    return {
        "claim_id": getattr(claim, "id", None),
        "trigger": trigger,
        "action": action,
        "rationale": rationale,
        "text": getattr(claim, "text", ""),
    }

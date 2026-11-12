"""Single policy point: which claim authors never reach ``confirmed`` automatically.

Operator decision 2026-10-13 (T-1354): claims whose ``source_agent`` is
``llm-stop-hook`` stay ``candidate``. A blind-labelled review rated 30% of the
hook's confirmed claims as obvious or low value, so no automatic path may
promote them. Only an explicit operator/agent call to the lifecycle API
(``transition_claim`` with an ``operator``/``agent`` actor) may still confirm
them; no MCP, CLI or dashboard surface exposes that today, and steward proposals
never target ``confirmed``.

Every automatic promoter consults this module: ``core.lifecycle.transition_claim``
enforces it for all lifecycle jobs (validator, jev revalidation, curation drain,
compaction), and the steward's raw-SQL confirm applies the same predicate.
``precompact-residual`` (T-1633) joins the default set: a residual is extracted by an
LLM from the turns a compaction is about to summarize, so its claims are leads for
review, not facts, and stay ``candidate`` like the stop hook's.
Configure the set with ``MEMORYMASTER_NON_AUTO_PROMOTABLE_SOURCES``
(comma-separated source agents; set it empty to disable the policy).

T-1591 adds a structural rule on the same gate: a predicate without a subject is
a shifted field (a category label in the predicate slot), not a triple, so it is
never auto-promoted, whoever wrote it. A subject with a predicate and no object
is NOT malformed: it is the normal shape of dream-worker output. (The loopback
"facts" that motivated T-1591 are fixed at the source, in
``govern.jobs.extractor.infer_structure``, not blocked here.)

T-2112 adds a content rule on the same gate: a transactional customer balance (an
identifiable customer, a balance/debt term and a currency amount or an explicit zero
balance, see :func:`states_customer_balance`) is never auto-promoted either. It is
operational data about a person, not durable knowledge, and stays ``candidate``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any

ENV_NON_AUTO_PROMOTABLE_SOURCES = "MEMORYMASTER_NON_AUTO_PROMOTABLE_SOURCES"
DEFAULT_NON_AUTO_PROMOTABLE_SOURCES = frozenset({"llm-stop-hook", "precompact-residual"})

# Lifecycle event payload ``actor`` values that mark an explicit decision rather
# than an automatic cycle (cycles stamp ``automation``, see govern.steward).
EXPLICIT_ACTORS = frozenset({"operator", "agent"})


class PromotionPolicyBlocked(ValueError):
    """An automatic path tried to confirm a claim its author may not auto-promote."""


def non_auto_promotable_sources() -> frozenset[str]:
    raw = os.environ.get(ENV_NON_AUTO_PROMOTABLE_SOURCES)
    if raw is None:
        return DEFAULT_NON_AUTO_PROMOTABLE_SOURCES
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def auto_promotion_blocked(source_agent: str | None) -> bool:
    agent = str(source_agent or "").strip()
    return bool(agent) and agent in non_auto_promotable_sources()


def _blank(value: object) -> bool:
    return not str(value or "").strip()


def is_malformed_triple(subject: object, predicate: object) -> bool:
    """A predicate with no subject: the category label landed in the wrong slot."""
    return not _blank(predicate) and _blank(subject)


def structure_blocks_auto_promotion(subject: object, predicate: object) -> bool:
    return is_malformed_triple(subject, predicate)


# --- customer balance (T-2112) -----------------------------------------------------------------
# All three parts must co-occur inside one sentence-sized window around a balance term, so a field
# name, a policy threshold or an aggregate never matches on one part alone.
_WINDOW = 100  # chars each side of the balance term (about one sentence)

_BALANCE_TERM = re.compile(
    r"\b(?:saldo|balance|deuda|adeud\w*|debt|outstanding|owes?|owed|impag\w+|unpaid|overdue)\b", re.I
)
_AMOUNT = re.compile(
    r"(?:(?<![A-Za-z])(?:ARS|USD|EUR|U\$S|US\$|AR\$)|[$€])\s*\d"
    r"|\d[\d.,]*\s*(?:pesos|ars|usd|eur|euros|d[oó]lares|dollars)\b",
    re.I,
)
_ZERO = re.compile(r"\b(?:zero|cero)\b", re.I)
_ID_MARKER = re.compile(
    r"\b(?:cliente|client|customer|socio|uisp)\b[\s:#]*"
    r"(?:(?:n[°º]?\.?|nro\.?|num\w*|number|id|c[oó]digo|code)\b[\s:#.]*)?\d+",
    re.I,
)
# A comparison, condition or aggregate is a rule or a total, not one customer's account.
# Only weighs against a party known by NAME; a client-id marker is stronger evidence.
_RULE_CUE = re.compile(
    r"[<>≤≥]|\b(?:under|below|above|less than|more than|at least|threshold|minimum|average"
    r"|total|prepa\w+|promedio|umbral|menos de|al menos|m[ií]nimo|en total)\b",
    re.I,
)
_NAME_TOKEN = re.compile(r"[A-ZÁÉÍÓÚÑ][a-záéíóúñ'’.-]+")
# Corporate suffixes: not a person, and capitalised like one.
_ORG_TOKENS = frozenset({
    "banco", "bank", "inc", "corp", "srl", "sa", "sas", "llc", "ltd", "gmbh", "group", "grupo",
    "company", "services", "servicios", "cloud", "technologies", "systems",
})


def _person_like(subject: object) -> bool:
    """1-4 Capitalised name tokens, no digits, no corporate suffix (``API``/``OpenAI`` are not)."""
    tokens = str(subject or "").split()
    return (
        1 <= len(tokens) <= 4
        and all(_NAME_TOKEN.fullmatch(t) for t in tokens)
        and not any(t.strip(".").lower() in _ORG_TOKENS for t in tokens)
    )


def _amount_or_zero(text: str) -> bool:
    return bool(_AMOUNT.search(text) or _ZERO.search(text))


def states_customer_balance(subject: object, predicate: object, object_value: object, text: object) -> bool:
    """True when the claim states what an identifiable customer owes / has outstanding / has as balance.

    A CONJUNCTION is required: a balance or debt term, a currency-marked amount (or an explicit
    zero balance) and an identified party, all within one sentence-sized window of the text. The
    party is a client-id marker (``cliente 9001``, ``customer #9001``, ``uisp 9001``) or a
    person-like ``subject`` that appears in the window. Triple form: a person-like subject, a
    predicate with balance wording and an amount in ``object_value`` or ``text``. A balance term
    alone (``saldo_pendiente`` field docs, ``deuda tecnica``), a policy threshold, an aggregate or a
    bank's account condition does not match. Judgement is by wording; the known limits are in
    ``.claude/rules/claims-lifecycle.md``.
    """
    body = str(text or "")
    name = str(subject or "").strip()
    person = _person_like(name)
    claim_wording = f"{predicate or ''} {object_value or ''}"
    predicate_words = re.sub(r"([a-z])([A-Z])", r"\1 \2", str(predicate or "")).replace("_", " ")
    if (
        person
        and _BALANCE_TERM.search(predicate_words)
        and _amount_or_zero(f"{object_value or ''} {body}")
        and not _RULE_CUE.search(f"{predicate_words} {claim_wording} {body}")
    ):
        return True
    for term in _BALANCE_TERM.finditer(body):
        window = body[max(0, term.start() - _WINDOW): term.end() + _WINDOW]
        if not _amount_or_zero(window):
            continue
        if _ID_MARKER.search(window):
            return True
        if person and name.lower() in window.lower() and not _RULE_CUE.search(window):
            return True
    return False


def customer_balance_blocks_auto_promotion(claim: Any) -> bool:
    get = claim.get if isinstance(claim, Mapping) else lambda name: getattr(claim, name, None)
    return states_customer_balance(get("subject"), get("predicate"), get("object_value"), get("text"))


def claim_auto_promotion_blocked(claim: Any) -> bool:
    """Source, structure OR customer-balance policy, read off a claim-like object or row mapping."""
    get = claim.get if isinstance(claim, Mapping) else lambda name: getattr(claim, name, None)
    return (
        auto_promotion_blocked(get("source_agent"))
        or structure_blocks_auto_promotion(get("subject"), get("predicate"))
        or customer_balance_blocks_auto_promotion(claim)
    )


def is_explicit_actor(event_payload: dict[str, object] | None) -> bool:
    return str((event_payload or {}).get("actor") or "").strip().lower() in EXPLICIT_ACTORS

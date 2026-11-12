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
# Three parts must co-occur around a balance term, so a field name, a policy threshold or an
# aggregate never matches on one part alone. Every quantifier is bounded and only the first
# _MAX_SCAN characters / _MAX_TERMS balance terms are judged, so a hostile claim costs linear time.
_MAX_SCAN = 20_000  # shortcut: text past this is not judged (a balance statement is a sentence), raise if lessons outgrow it
_MAX_TERMS = 50  # balance terms examined per claim
_WINDOW = 100  # chars each side of the balance term (about one sentence)
_ID_NEAR = 200  # a client id counts as the party only this close to the balance term (or inside the subject)

_BALANCE_TERM = re.compile(
    r"\b(?:saldo|balance|deuda|adeud\w{0,10}|debt|outstanding|owes?|owed|impag\w{0,8}|unpaid|overdue)\b"
    r"|\bdebe\b(?=\s{1,3}(?:[$€]|(?:ARS|USD|EUR)\b))",  # bare "debe" is a common modal verb
    re.I,
)
_AMOUNT = re.compile(
    r"(?:(?<![A-Za-z])(?:ARS|USD|EUR|U\$S|US\$|AR\$)|[$€])\s{0,3}\d"
    r"|(?<![\d.,])\d[\d.,]{0,20}\s{0,3}(?:pesos|ars|usd|eur|euros|d[oó]lares|dollars)\b",
    re.I,
)
# A decimal amount with no currency symbol ("deuda de 12500.00"): only counts next to a client id.
_BARE_AMOUNT = re.compile(
    r"(?<![\d.,-])\d{1,9}(?:[.,]\d{3}){0,3}[.,]\d{2}(?!\d)"
    r"(?!\s{0,2}(?:%|(?:ms|s|seg|min|h|hs|px|loc|mb|kb|gb|x)\b))",  # "12.50 ms" is a measure, not money
    re.I,
)
_ZERO = re.compile(r"\b(?:zero|cero)\b", re.I)
_ID_MARKER = re.compile(
    r"\b(?:cliente|client|customer|socio|uisp)\b[\s:#.(]{0,4}"
    r"(?:(?:n\.?[°º]|no\.?|nro\.?|n[uú]m(?:ero|ber)?\.?|id|c[oó]digo|code)[\s:#.(]{0,4})?\d"
    # a name between the word and the id: "client Juan Perez #9001"
    r"|\b(?:cliente|client|customer|socio|uisp)\b[^\d#\n]{0,40}#\s{0,2}\d",
    re.I,
)
# A comparison, condition or aggregate is a rule or a total, not one customer's account. Each cue is
# anchored to the amount or the balance term, so "a total pending balance of ..." is not one.
# It weighs against a party known by NAME or by a client id away from the balance term; a client id
# next to the balance term is stronger evidence.
_CUR_OPT = r"(?:ARS|USD|EUR|U\$S|US\$|AR\$|[$€])?"
_RULE_CUE = re.compile(
    # comparator directly before the amount: "under $1,000", "> $0"
    r"(?:[<>≤≥]|\b(?:under|below|above|less than|more than|at least|menos de|al menos)\b)"
    r"\s{0,3}=?\s{0,3}" + _CUR_OPT + r"\s{0,3}\d"
    # qualifier just before the balance term: "average account balance", "prepay balance"
    r"|\b(?:average|minimum|maximum|prepay|prepaid|prepago|promedio|m[ií]nim[oa]|threshold|umbral)\s{1,3}"
    r"(?:[\w-]{1,30}\s{1,3}){0,2}(?:saldo|balance|deuda|debt)\b"
    # a counted plural is an aggregate: "38 suspended clients owe ..."
    r"|(?<![\d.,])\d[\d.,]{0,20}\s{1,3}(?:[\w-]{1,30}\s{1,3}){0,2}"
    r"(?:clients|customers|clientes|socios|users|usuarios|accounts|cuentas)\b",
    re.I,
)
_CAMEL_BOUNDARY =re.compile(r"([a-z])([A-Z])")
_NAME_TOKEN = re.compile(r"[A-ZÁÉÍÓÚÑ][a-záéíóúñ'’-]+|[A-ZÁÉÍÓÚÑ]\.")
_CAPS_TOKEN = re.compile(r"[A-ZÁÉÍÓÚÑ]{3,}")
_PARTICLES = frozenset({"de", "del", "la", "las", "los", "van", "von", "da", "di", "dos", "do", "el"})
# ALLCAPS tokens that are acronyms or products, not a surname.
_ACRONYMS = frozenset({
    "api", "uisp", "crm", "erp", "sql", "cpu", "gpu", "usd", "ars", "eur", "mcp", "llm", "url", "dns",
    "vpn", "iva", "cuit", "cuil", "afip", "pdf", "csv", "json", "html", "ssh", "vps", "nas", "wip",
})
# Corporate suffixes: not a person, and capitalised like one.
_ORG_TOKENS = frozenset({
    "banco", "bank", "inc", "corp", "srl", "sa", "sas", "llc", "ltd", "gmbh", "group", "grupo",
    "company", "services", "servicios", "cloud", "technologies", "systems",
})


def _person_like(subject: object) -> bool:
    """1-4 name tokens (Capitalised, an initial, or an ALLCAPS surname), particles (de la) between them.

    No digits, no corporate suffix, no acronym (``API``/``UISP``/``OpenAI`` are not people). A name with
    an ALLCAPS token needs two name tokens: a lone ``SANTANDER`` is a company.
    """
    tokens = str(subject or "").split()
    if not 1 <= len(tokens) <= 7 or tokens[0].lower() in _PARTICLES or tokens[-1].lower() in _PARTICLES:
        return False
    names = [t for t in tokens if t.lower() not in _PARTICLES]
    if not 1 <= len(names) <= 4 or any(t.strip(".").lower() in _ORG_TOKENS for t in names):
        return False
    caps = [t for t in names if _CAPS_TOKEN.fullmatch(t)]
    if caps and len(names) < 2:
        return False
    return all(
        _NAME_TOKEN.fullmatch(t) or (_CAPS_TOKEN.fullmatch(t) and t.lower() not in _ACRONYMS) for t in names
    )


def _amount_or_zero(text: str) -> bool:
    return bool(_AMOUNT.search(text) or _ZERO.search(text))


def states_customer_balance(subject: object, predicate: object, object_value: object, text: object) -> bool:
    """True when the claim states what an identifiable customer owes / has outstanding / has as balance.

    A CONJUNCTION is required around a balance or debt term: a currency-marked amount (or an explicit
    zero balance) within one sentence-sized window, AND a party. The party is a client-id marker
    (``cliente 9001``, ``Cliente N° 9001``, ``customer #9001``, ``uisp 9001``) inside the term's window, or
    within ``_ID_NEAR`` characters of the term or in the subject (then a rule cue in the window excuses
    it; a bare decimal amount, never one followed by a unit, counts only with the id inside the
    window), or a person-like ``subject`` that appears in the window. An id mentioned farther away in a
    long note is just a mention. Triple form: a
    person-like subject, balance wording in the predicate and an amount in ``object_value`` or text.
    A balance term alone (``saldo_pendiente`` field docs, ``deuda tecnica``), a policy threshold, an
    aggregate or a bank's account condition does not match. Judgement is by wording; the known limits
    are in ``.claude/rules/claims-lifecycle.md``.
    """
    body = str(text or "")[:_MAX_SCAN]
    name = str(subject or "").strip()[:200]
    obj = str(object_value or "")[:200]
    person = _person_like(name)
    if person:
        predicate_words = _CAMEL_BOUNDARY.sub(r"\1 \2", str(predicate or "")[:200]).replace("_", " ")
        if (
            _BALANCE_TERM.search(predicate_words)
            and _amount_or_zero(f"{obj} {body}")
            and not _RULE_CUE.search(f"{predicate_words} {obj}")
        ):
            return True
    id_in_subject = bool(_ID_MARKER.search(name))
    for count, term in enumerate(_BALANCE_TERM.finditer(body)):
        if count >= _MAX_TERMS:
            break
        window = body[max(0, term.start() - _WINDOW): term.end() + _WINDOW]
        id_in_window = bool(_ID_MARKER.search(window))
        bare = id_in_window and _BARE_AMOUNT.search(body[term.end(): term.end() + 40])
        if not (bare or _amount_or_zero(window)):
            continue
        if id_in_window:
            return True
        if _RULE_CUE.search(window):
            continue
        near = body[max(0, term.start() - _ID_NEAR): term.end() + _ID_NEAR]
        if id_in_subject or _ID_MARKER.search(near) or (person and name.lower() in window.lower()):
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

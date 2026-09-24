"""S5 SKILLS: TypeSafe Jev skill suggestion on the shared decision engine.

This module only proposes skill claim IDs.  The caller owns SQLite authority,
scope checks, sensitivity filtering and post-selection rehydration.  Requests
go through :mod:`memorymaster.decisions` (one in-process transport, one egress
redactor, one ledger); there is no child process, so nothing in the working
directory can intercept the key (review F-15).

Two rounds, each one logged decision on surface ``skills``:

1. *wide*: the cookbook gate ``skills.needs_procedure`` plus ``skills.which_skill``
   (Choice over every authorized skill's title / when_to_use / when_not_to_use and
   ``none``).  Gate below its ``gate`` threshold or a confident ``none`` means no
   skill; otherwise the three most probable skills are shortlisted.
2. *detailed*: ``skills.which_skill`` over the shortlist with each workflow, decision
   rules and validation, plus ``skills.fits`` per shortlisted skill.  A confident
   ``none`` means no skill; a winner must be the most probable option, confident,
   and its own fit (never another candidate's) must reach the ``fit`` threshold.

Low confidence or low fit keeps the deterministic (legacy) selection; a
contradictory answer is a logged ``choose_error``.  Items are ``claim:<id>``.

A request withheld before the engine (see :data:`WITHHELD_REASONS`: an empty,
oversized or scanner-flagged query, a private or oversized catalog, an incomplete
or path-bearing descriptor) sends nothing and is still logged, by
:func:`record_withheld`, as one fallback row with no request or catalog text: the
label and at most one ``claim:<id>`` go in ``baseline_features``.

Return value of :func:`select_skill_ids`: ``None`` keeps the caller's legacy
selection (off, shadow, any fallback or deferral); ``[]`` is a live, confident
"no skill applies"; ``[id]`` is Jev's live pick.

Mode: ``MEMORYMASTER_JEV_SKILLS`` / ``MEMORYMASTER_JEV_MODE`` (see
``docs/jev-decisions.md``).  Backward compatibility: when neither is set, the
pre-4.9 ``MEMORYMASTER_JEV_SKILLS_ENABLED`` flag (truthy) means ``live``.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import threading
import time
import uuid
from typing import Any, Callable, Mapping, Sequence

from memorymaster.decisions import questions
from memorymaster.decisions.config import MODE_ENV, DecisionConfig, env_truthy
from memorymaster.decisions.engine import (
    Answers,
    DecisionContext,
    DecisionEngine,
    DecisionItem,
    JevChoice,
    default_engine,
)

SURFACE = "skills"
LEGACY_ENABLED_ENV = "MEMORYMASTER_JEV_SKILLS_ENABLED"
GATE = "skills.needs_procedure"
WHICH = "skills.which_skill"
FITS = "skills.fits"
_NONE = "none"
_NONE_WIDE = "No authorized skill is directly applicable to this request."
_NONE_DETAILED = "No shortlisted skill directly applies to this request."
_MAX_SKILLS = 200  # Choice supports 255 options; reserve one explicit abstention.
_SHORTLIST = 3
_MAX_QUERY_CHARS = 4_000
_MAX_SUMMARY_CHARS = 320
_MAX_LIST_ITEMS = 8
_MAX_PAYLOAD_BYTES = 64 * 1024
_MIN_CONFIDENCE = 0.70
_MIN_FIT = 0.70
_GATE = 0.50
# A drive letter must not end a word (``https://`` is not ``s:/``).  URLs are NOT
# exempt: the egress redactor leaves ``smb://nas/home/<user>`` in clear, so a home
# or system directory inside a URL path withholds too (S5-URL-HOME-PATH-EGRESS).
_PATH_RE = re.compile(r"(?:(?<![A-Za-z0-9])[A-Za-z]:[\\/]|\\\\|/(?:home|users|var|tmp|etc|opt)/)", re.IGNORECASE)
_BRIEF_FIELDS = ("title", "when_to_use", "when_not_to_use")
_DETAIL_FIELDS = (*_BRIEF_FIELDS, "workflow", "decision_rules", "validation")


# Fallbacks decided before the engine: label -> ``fallback_reason``.
WITHHELD_REASONS: Mapping[str, str] = {
    "query_empty": "invalid_request",
    "query_too_long": "request_too_large",
    "query_sensitive": "egress_blocked",  # flagged by the ingest scanner (skills.recall_skills)
    "catalog_empty": "invalid_request",
    "catalog_too_large": "request_too_large",
    "catalog_private": "egress_blocked",  # a non-public skill (skills.recall_skills)
    "payload_too_large": "request_too_large",
    "descriptor_invalid": "invalid_request",
    "descriptor_path": "egress_blocked",
}


class _Contradiction(ValueError):
    """An answer that contradicts itself; the engine logs it as ``choose_error``."""


class _Withheld(ValueError):
    """The request must not be sent; ``label`` is a :data:`WITHHELD_REASONS` key."""

    def __init__(self, label: str, claim_id: object = None) -> None:
        super().__init__(label)
        self.label = label
        self.claim_id = claim_id if type(claim_id) is int and claim_id > 0 else None


def skills_config(environ: Mapping[str, str] | None = None) -> DecisionConfig:
    """The decision config with the legacy ``MEMORYMASTER_JEV_SKILLS_ENABLED`` flag applied.

    Precedence: ``MEMORYMASTER_JEV_SKILLS`` > ``MEMORYMASTER_JEV_MODE`` > legacy flag
    (truthy -> ``live``) > ``off``, so the global kill switch also stops the old flag.
    A falsy legacy flag changes nothing.
    """
    source = os.environ if environ is None else environ
    config = DecisionConfig.from_env(source)
    if (SURFACE not in config.surface_modes and not (source.get(MODE_ENV) or "").strip()
            and env_truthy(source.get(LEGACY_ENABLED_ENV))):
        config = dataclasses.replace(config, surface_modes={**config.surface_modes, SURFACE: "live"})
    return config


_legacy_engine: DecisionEngine | None = None
_legacy_lock = threading.Lock()


def _engine() -> DecisionEngine:
    """The process-wide engine, or one built for the legacy flag's config."""
    config = skills_config()
    engine = default_engine()
    if engine.config == config:
        return engine
    global _legacy_engine
    with _legacy_lock:
        if _legacy_engine is None or _legacy_engine.config != config:
            _legacy_engine = DecisionEngine(config)
        return _legacy_engine


_HASHED_SESSION_KEY = re.compile(r"session:[0-9a-f]{64}")


def _ledger_session_key(session_key: object, tenant: str | None) -> str | None:
    """F-11: log the normalized, tenant-bound session hash, never a raw session id.

    A key already produced by ``recall.jev_surfaces.decision_session_key`` is kept;
    anything else is hashed through it with ``tenant``.
    """
    if not isinstance(session_key, str) or not session_key.strip():
        return None
    if _HASHED_SESSION_KEY.fullmatch(session_key):
        return session_key
    try:
        from memorymaster.recall.jev_surfaces import decision_session_key

        return decision_session_key(session_key, tenant)
    except Exception:  # noqa: BLE001 - no key rather than a raw one
        return None


def select_skill_ids(
    query: str,
    skills: list[dict[str, Any]],
    *,
    legacy_ids: Sequence[int] = (),
    engine: DecisionEngine | None = None,
    scope: str | None = None,
    tenant: str | None = None,
    session_key: str | None = None,
    deliverable: Callable[[list[int]], list[int]] | None = None,
) -> list[int] | None:
    """Return Jev's live pick (``[id]``), a live confident ``[]``, or ``None`` for legacy.

    ``deliverable(ids)`` returns the ids the caller would deliver right now (its
    SQLite re-authorization); it only sets ``delivered`` in the ledger.
    """
    started = time.monotonic()
    run = engine or _engine()
    session_key = _ledger_session_key(session_key, tenant)
    try:
        catalog = _normalize_catalog(query, skills)
        if _payload_bytes(query, catalog) > _MAX_PAYLOAD_BYTES:
            raise _Withheld("payload_too_large")
    except _Withheld as withheld:
        record_withheld(withheld.label, catalog_size=len(skills) if isinstance(skills, list) else 0,
                        legacy_ids=legacy_ids, withheld_id=withheld.claim_id, engine=run, scope=scope,
                        tenant=tenant, session_key=session_key, deliverable=deliverable, started=started)
        return None
    legacy = [_ref(claim_id) for claim_id in dict.fromkeys(legacy_ids) if claim_id in catalog]
    legacy_rank = {ref: index + 1 for index, ref in enumerate(legacy)}
    delivery_filter = _delivery_filter(deliverable)

    def context(round_name: str, legacy_exposed: list[str], **features: Any) -> DecisionContext:
        return DecisionContext(kind="hook", session_key=session_key, scope=scope, tenant=tenant,
                               exploration="none", legacy_exposed=legacy_exposed, delivery_filter=delivery_filter,
                               baseline_features={"round": round_name, "catalog_size": len(catalog), **features})

    wide_options = ((_NONE, _NONE_WIDE),) + tuple(
        (_option_id(claim_id), _view(skill, _BRIEF_FIELDS)) for claim_id, skill in catalog.items())
    first = run.decide(
        SURFACE,
        state={"request": query},
        questions=[questions.BoundQuestion(questions.get(GATE)),
                   questions.BoundQuestion(questions.get(WHICH), options=wide_options)],
        items=[DecisionItem(_ref(cid), "skill", legacy_rank.get(_ref(cid))) for cid in catalog],
        legacy_action=legacy,
        choose=lambda answers: _choose_wide(answers, legacy),
        context=context("wide", legacy),
    )
    plan = _jev_plan(first)
    if not (isinstance(plan, Mapping) and isinstance(plan.get("shortlist"), list)):
        return _result(first, legacy)
    shortlist = [ref for ref in plan["shortlist"] if _parse_ref(ref) in catalog]
    if not shortlist:
        return None
    detail_options = ((_NONE, _NONE_DETAILED),) + tuple(
        (_option_id(_parse_ref(ref)), _view(catalog[_parse_ref(ref)], _DETAIL_FIELDS)) for ref in shortlist)
    bound = [questions.BoundQuestion(questions.get(WHICH), options=detail_options)]
    bound.extend(questions.BoundQuestion(questions.get(FITS), ref, {"skill": _view(catalog[_parse_ref(ref)],
                                                                                  _DETAIL_FIELDS)})
                 for ref in shortlist)
    # A shadow first round already logged the legacy exposure; count it once.
    exposed_by_legacy = legacy if first.mode == "live" else []
    second = run.decide(
        SURFACE,
        state={"request": query},
        questions=bound,
        items=[DecisionItem(ref, "skill", legacy_rank.get(ref)) for ref in dict.fromkeys([*shortlist, *legacy])],
        legacy_action=legacy,
        choose=lambda answers: _choose_detailed(answers, shortlist, legacy),
        context=context("detailed", exposed_by_legacy, wide_decision_id=first.decision_id),
    )
    return _result(second, legacy)


def record_withheld(
    label: str,
    *,
    catalog_size: int = 0,
    legacy_ids: Sequence[int] = (),
    withheld_id: int | None = None,
    engine: DecisionEngine | None = None,
    scope: str | None = None,
    tenant: str | None = None,
    session_key: str | None = None,
    deliverable: Callable[[list[int]], list[int]] | None = None,
    started: float | None = None,
) -> bool:
    """Log one skills decision that fell back before the engine; nothing was sent.

    The row has the ``fallback_reason`` of ``label`` (:data:`WITHHELD_REASONS`), the
    legacy selection as action, ``not_sent`` and no request, state or catalog text;
    ``baseline_features`` holds the label and at most one ``claim:<id>``.  Like the
    engine, ``off`` writes nothing unless ``MEMORYMASTER_DECISIONS_LOG_OFF`` is set.
    Attempt count 0 keeps these rows out of the breaker and the RPM budget.  Never raises.
    """
    try:
        run = engine or _engine()
        mode = run.config.mode_for(SURFACE)
        if mode == "off" and not run.config.log_off:
            return False
        session_key = _ledger_session_key(session_key, tenant)
        from memorymaster.decisions.engine import fallback_item_rows, fallback_record

        legacy = [_ref(cid) for cid in dict.fromkeys(legacy_ids) if type(cid) is int and cid > 0]
        check = _delivery_filter(deliverable)
        delivered = set(check(legacy) if check is not None else legacy)
        features: dict[str, Any] = {"round": "wide", "catalog_size": int(catalog_size), "withheld": label}
        if type(withheld_id) is int and withheld_id > 0:
            features["withheld_ref"] = _ref(withheld_id)
        decision_id = uuid.uuid4().hex
        elapsed = 0 if started is None else max(0, int(round((time.monotonic() - started) * 1000)))
        record = fallback_record(
            decision_id, SURFACE, mode=mode, reason="mode_off" if mode == "off" else WITHHELD_REASONS[label],
            legacy_action=legacy, baseline_features=features, session_key=session_key, scope=scope, tenant=tenant,
            engine_ms=elapsed,
        )
        items = fallback_item_rows(decision_id, legacy, kind="skill", delivered=delivered)
        return bool(run.ledger.write_decision(record, items))
    except Exception:  # telemetry must never break recall
        return False


def _delivery_filter(deliverable: Callable[[list[int]], list[int]] | None) -> Callable[[list[str]], list[str]] | None:
    """Adapt the caller's id check to the engine's ``delivery_filter`` over refs."""
    if deliverable is None:
        return None

    def delivery_filter(refs: list[str]) -> list[str]:
        ids = [claim_id for claim_id in (_parse_ref(ref) for ref in refs) if claim_id is not None]
        try:
            allowed = set(deliverable(ids)) if ids else set()
        except Exception:
            return list(refs)  # like the engine: an unknown delivery is recorded as exposed
        return [ref for ref in refs if _parse_ref(ref) in allowed]

    return delivery_filter


def _jev_plan(decision: Any) -> Any:
    """What Jev's policy chose in round one (live: the action; shadow: the logged Jev action)."""
    if decision.fallback_reason is not None:
        return None
    if decision.mode == "live":
        return decision.action
    if decision.mode == "shadow":
        return decision.jev_action
    return None


def _result(decision: Any, legacy: list[str]) -> list[int] | None:
    if decision.mode != "live" or decision.fallback_reason is not None:
        return None
    action = decision.action
    if not isinstance(action, list) or action == legacy:
        return None
    ids = [_parse_ref(ref) for ref in action]
    if any(claim_id is None for claim_id in ids) or len(set(ids)) != len(ids) or len(ids) > 1:
        return None
    return ids


# ------------------------------------------------------------------ policy ---

def _choose_wide(answers: Answers, legacy: list[str]) -> JevChoice:
    gate = answers.noul(GATE)
    if gate is None:
        raise _Contradiction("gate unanswered")
    if gate < answers.threshold(GATE, "gate", _GATE):
        return JevChoice(action=[], exposed=[])
    which = _checked_choice(answers)
    if not _confident(answers, which):
        return JevChoice(action=list(legacy))
    if which.value == _NONE:
        return JevChoice(action=[], exposed=[])
    parsed = [(probability, _parse_option(option)) for option, probability in which.probabilities.items()
              if option != _NONE]
    if any(claim_id is None for _, claim_id in parsed):
        raise _Contradiction("unknown option")
    ranked = sorted(parsed, reverse=True)
    shortlist = [_ref(claim_id) for _, claim_id in ranked[:_SHORTLIST]]
    scores = {_ref(claim_id): probability for probability, claim_id in ranked[:_SHORTLIST]}
    return JevChoice(action={"shortlist": shortlist}, order=shortlist, scores=scores, exposed=[])


def _choose_detailed(answers: Answers, shortlist: list[str], legacy: list[str]) -> JevChoice:
    which = _checked_choice(answers)
    if not _confident(answers, which):
        return JevChoice(action=list(legacy))
    if which.value == _NONE:
        return JevChoice(action=[], exposed=[])
    ref = _ref(_parse_option(which.value) or 0)
    if ref not in shortlist:
        raise _Contradiction("winner not shortlisted")
    # Deliberately the winner's own fit: a high score for another candidate
    # cannot justify returning a winner that does not fit.
    fit = answers.noul(FITS, ref)
    if fit is None:
        raise _Contradiction("winner fit unanswered")
    if fit < answers.threshold(FITS, "fit", _MIN_FIT):
        return JevChoice(action=list(legacy))
    return JevChoice(action=[ref], exposed=[ref])


def _checked_choice(answers: Answers) -> Any:
    which = answers.get(WHICH)
    if which is None or which.primitive != "choice":
        raise _Contradiction("choice unanswered")
    if which.probabilities[which.value] != max(which.probabilities.values()):
        raise _Contradiction("choice is not the most probable option")
    return which


def _confident(answers: Answers, which: Any) -> bool:
    return which.confidence is not None and which.confidence >= answers.threshold(WHICH, "confidence",
                                                                                   _MIN_CONFIDENCE)


# ----------------------------------------------------------------- catalog ---

def _normalize_catalog(query: str, skills: list[dict[str, Any]]) -> dict[int, dict[str, object]]:
    """The validated catalog; raises :class:`_Withheld` for the first rule broken."""
    if not isinstance(query, str) or not query.strip():
        raise _Withheld("query_empty")
    if len(query) > _MAX_QUERY_CHARS:
        raise _Withheld("query_too_long")
    if not isinstance(skills, list) or not skills:
        raise _Withheld("catalog_empty")
    if len(skills) > _MAX_SKILLS:
        raise _Withheld("catalog_too_large")
    result: dict[int, dict[str, object]] = {}
    for skill in skills:
        if not isinstance(skill, Mapping):
            raise _Withheld("descriptor_invalid")
        claim_id = skill.get("claim_id")
        if type(claim_id) is not int or claim_id <= 0 or claim_id in result:
            raise _Withheld("descriptor_invalid", claim_id)
        result[claim_id] = {
            "title": _clean_text(skill.get("title"), claim_id),
            "when_to_use": _clean_text(skill.get("when_to_use"), claim_id),
            "when_not_to_use": _clean_text(skill.get("when_not_to_use"), claim_id),
            "workflow": _clean_list(skill.get("workflow"), claim_id),
            "decision_rules": _clean_list(skill.get("decision_rules"), claim_id),
            "validation": _clean_list(skill.get("validation"), claim_id),
        }
    return result


def _clean_text(value: object, claim_id: int) -> str:
    if not isinstance(value, str):
        raise _Withheld("descriptor_invalid", claim_id)
    text = " ".join(value.split())
    if not text or len(text) > _MAX_SUMMARY_CHARS:
        raise _Withheld("descriptor_invalid", claim_id)
    if _has_path(text):
        raise _Withheld("descriptor_path", claim_id)
    return text


def _clean_list(value: object, claim_id: int) -> list[str]:
    if not isinstance(value, list) or len(value) > _MAX_LIST_ITEMS:
        raise _Withheld("descriptor_invalid", claim_id)
    return [_clean_text(item, claim_id) for item in value]


def _has_path(text: str) -> bool:
    """A local filesystem path (drive, UNC, POSIX system/home dir), inside a URL or not."""
    return bool(_PATH_RE.search(text))


def _view(skill: Mapping[str, object], fields: Sequence[str]) -> dict[str, object]:
    """Only descriptor fields leave the machine; lineage, scope and citations never do."""
    return {name: skill[name] for name in fields}


def _payload_bytes(query: str, catalog: Mapping[int, Mapping[str, object]]) -> int:
    wide = {_option_id(claim_id): _view(skill, _BRIEF_FIELDS) for claim_id, skill in catalog.items()}
    encoded = json.dumps({"request": query, "criteria": wide}, ensure_ascii=False, separators=(",", ":"))
    return len(encoded.encode("utf-8"))


def _ref(claim_id: int) -> str:
    return f"claim:{claim_id}"


def _parse_ref(ref: object) -> int | None:
    if not isinstance(ref, str) or not ref.startswith("claim:"):
        return None
    return _positive_int(ref.removeprefix("claim:"))


def _option_id(claim_id: int) -> str:
    return f"skill:{claim_id}"


def _parse_option(value: str) -> int | None:
    if not value.startswith("skill:"):
        return None
    return _positive_int(value.removeprefix("skill:"))


def _positive_int(raw: str) -> int | None:
    try:
        parsed = int(raw)
    except ValueError:
        return None
    return parsed if raw == str(parsed) and parsed > 0 else None


__all__ = ["LEGACY_ENABLED_ENV", "SURFACE", "WITHHELD_REASONS", "record_withheld", "select_skill_ids",
           "skills_config"]

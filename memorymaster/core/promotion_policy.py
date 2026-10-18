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
Configure the set with ``MEMORYMASTER_NON_AUTO_PROMOTABLE_SOURCES``
(comma-separated source agents; set it empty to disable the policy).

T-1591 adds a structural rule on the same gate. ``govern.jobs.extractor`` stamps
``server / ip_address / <first IPv4 in the text>`` on any prose that mentions an
address, so long legitimate claims that only cite 127.0.0.1 carry a loopback
"fact" as their structured object; and a predicate without a subject is a
shifted field, not a triple. Neither is auto-promoted, whoever wrote it. A
subject with a predicate and no object is NOT malformed: it is the normal shape
of dream-worker output. Private (non-loopback) addresses stay promotable on
purpose, see ``core/security.py``.
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

ENV_NON_AUTO_PROMOTABLE_SOURCES = "MEMORYMASTER_NON_AUTO_PROMOTABLE_SOURCES"
DEFAULT_NON_AUTO_PROMOTABLE_SOURCES = frozenset({"llm-stop-hook"})

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


def is_loopback_object(value: object) -> bool:
    """True for a structured object that points at this machine.

    Accepts a bare host or IP (``localhost``, ``127.0.0.1``, ``::1``, ``[::1]``),
    ``host:port`` and ``scheme://host[:port]/path``. Free prose is never loopback.
    """
    text = str(value or "").strip().lower()
    if not text or any(ch.isspace() for ch in text):
        return False
    try:
        return ipaddress.ip_address(text.strip("[]")).is_loopback
    except ValueError:
        pass
    try:
        host = urlsplit(text if "://" in text else f"//{text}").hostname
    except ValueError:
        return False
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def is_malformed_triple(subject: object, predicate: object) -> bool:
    """A predicate with no subject: the category label landed in the wrong slot."""
    return not _blank(predicate) and _blank(subject)


def structure_blocks_auto_promotion(subject: object, predicate: object, object_value: object) -> bool:
    return is_loopback_object(object_value) or is_malformed_triple(subject, predicate)


def claim_auto_promotion_blocked(claim: Any) -> bool:
    """Source policy OR structure policy, read off a claim-like object or row mapping."""
    get = claim.get if isinstance(claim, Mapping) else lambda name: getattr(claim, name, None)
    return auto_promotion_blocked(get("source_agent")) or structure_blocks_auto_promotion(
        get("subject"), get("predicate"), get("object_value")
    )


def is_explicit_actor(event_payload: dict[str, object] | None) -> bool:
    return str((event_payload or {}).get("actor") or "").strip().lower() in EXPLICIT_ACTORS

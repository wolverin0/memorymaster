"""Project brief: a fixed-shape, source-tagged, byte-capped digest of one project's live state.

Pure rendering over already collected inputs. Collection (ledger, claims, due-measurements,
ROADMAP) lives in project_repository; publication in project_projection. The SessionStart
hook only reads the published bytes, it never calls into this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from memorymaster.profile.project_repository import (
    DUE_SLUGS,
    _display,
    load_brief_claims,
    load_due_failures,
)

BRIEF_MAX_BYTES = 1600
STALE_DAYS = 14
RUNNING_CARDS = DECISIONS = FAILURES = 3
_RANK = {"running": 0, "review": 0, "ready": 1, "queued": 1}
_FAIL_STATES = ("failed", "blocked")
NL = chr(10)
TITLES = ("RUNNING", "NEXT", "DECISIONS", "FAILURES")
EMPTY = "empty"  # readable source with nothing in it, distinct from unavailable (None)


@dataclass(frozen=True)
class Entry:
    """One claim-, card- or measurement-derived line: when (for ordering), text, source tag."""

    when: datetime | None
    text: str
    tag: str


@dataclass(frozen=True)
class BriefInputs:
    project: str
    as_of: datetime
    cards: tuple[Mapping[str, Any], ...] = ()  # id, title, state, next_action, blocker, updated_at
    next_item: tuple[str, int] | None = None  # (text, 1-based ROADMAP line)
    roadmap_missing: bool = True
    decisions: tuple[Entry, ...] = ()
    failures: tuple[Entry, ...] = ()
    # name -> last-update instant | None (unavailable) | EMPTY. Order is the header order.
    sources: Mapping[str, datetime | None | str] | None = None


def when(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        instant = value
    else:
        try:
            instant = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return instant if instant.tzinfo else instant.replace(tzinfo=timezone.utc)


def cut_bottom(text: str) -> str:
    """Drop the last line (and any section title left orphaned); the header line always survives."""
    lines = text.split(NL)
    lines.pop()
    while len(lines) > 1 and lines[-1] in TITLES:
        lines.pop()
    return NL.join(lines)


def _newest(entries: list[Entry]) -> list[Entry]:
    floor = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(entries, key=lambda entry: entry.when or floor, reverse=True)


def _stamp(name: str, state: datetime | None | str, as_of: datetime) -> str:
    if state is None:
        return f"{name} unavailable"
    if isinstance(state, str):
        return f"{name} {state}"
    days = max(0, (as_of - state).days)
    return f"{name} (stale {days}d)" if days > STALE_DAYS else f"{name} {days}d"


def _field(label: str, value: Any, limit: int) -> str:
    text = _display(value, limit) if value else ""
    return f" | {label}: {text}" if text else ""


def _running(cards) -> list[str]:
    floor = datetime.min.replace(tzinfo=timezone.utc)
    live = [c for c in cards if c.get("state") in _RANK]
    live.sort(key=lambda c: (_RANK[c["state"]], -(when(c.get("updated_at")) or floor).timestamp(), str(c["id"])))
    return [
        f"- {_display(c['id'], 24)} {_display(c['state'], 12)}: {_display(c.get('title'), 60)}"
        f"{_field('next', c.get('next_action'), 60)}{_field('blocker', c.get('blocker'), 50)} [{_display(c['id'], 24)}]"
        for c in live[:RUNNING_CARDS]
    ]


def _card_failures(cards) -> list[Entry]:
    out = []
    for c in cards:
        state = c.get("state")
        reason = c.get("blocker") or (c.get("title") if state == "failed" else None)
        if state in _FAIL_STATES and reason:
            out.append(Entry(when(c.get("updated_at")), f"{_display(c['id'], 24)} {state}: {_display(reason, 80)}", _display(c["id"], 24)))
    return out


def _entry_line(entry: Entry, *, dated: bool) -> str:
    stamp = f"{entry.when.date().isoformat()} " if dated and entry.when else ""
    return f"- {stamp}{entry.text} [{entry.tag}]"


def render_brief(inputs: BriefInputs) -> str:
    """Header + RUNNING / NEXT / DECISIONS / FAILURES, every line ending in its source tag.

    Over the cap, whole lines are cut from the bottom (never mid-line, so a tag is never orphaned).
    """
    sources = inputs.sources or {}
    header = f"Brief {_display(inputs.project, 48)} as_of {inputs.as_of.date().isoformat()}"
    if sources:
        header += " | " + " | ".join(_stamp(name, state, inputs.as_of) for name, state in sources.items())
    if inputs.next_item:
        text, line = inputs.next_item
        nxt = [f"- {_display(text, 110)} [ROADMAP.md#L{line}]"]
    else:
        nxt = ["- none: no roadmap [ROADMAP.md]" if inputs.roadmap_missing else "- none: no open item [ROADMAP.md]"]
    failures = _newest([*_card_failures(inputs.cards), *inputs.failures])[:FAILURES]
    blocks = [
        ("RUNNING", _running(inputs.cards) or ["- none [ledger]"]),
        ("NEXT", nxt),
        ("DECISIONS", [_entry_line(e, dated=True) for e in _newest(list(inputs.decisions))[:DECISIONS]] or ["- none [claims]"]),
        ("FAILURES", [_entry_line(e, dated=False) for e in failures] or ["- none [claims]"]),
    ]
    text = NL.join([header, *(line for title, body in blocks for line in (title, *body))])
    while len(text.encode("utf-8")) > BRIEF_MAX_BYTES and NL in text:
        text = cut_bottom(text)
    return text


def collect_brief_inputs(config: Any, items: Mapping[str, Mapping[str, Any]], diagnostics: list[str],
                         now: datetime) -> BriefInputs:
    """Gather the brief's sources. Runs on the refresh path only, never in the SessionStart hook."""
    cards = tuple(
        {"id": item["support"]["id"], "state": item["support"]["state"], "title": item.get("title"),
         "next_action": item.get("next_action"), "blocker": item.get("blocker"), "updated_at": item.get("updated_at")}
        for key, item in items.items() if key.startswith("card:")
    )
    stamps = [t for t in (when(c["updated_at"]) for c in cards) if t]
    claims, claims_ok = load_brief_claims(config, now=now)
    decisions = tuple(Entry(r["when"], r["text"], f"mm-{r['id']}") for r in claims["decision"])
    failures = [Entry(r["when"], r["text"], f"mm-{r['id']}") for r in claims["failure"]]
    seen = [e.when for e in (*decisions, *failures) if e.when]
    sources: dict[str, datetime | None | str] = {
        "ledger": None if "ledger_unavailable" in diagnostics else max(stamps, default=EMPTY),
        "claims": max(seen, default=EMPTY) if claims_ok else None,
    }
    if config.slug in DUE_SLUGS:
        rows, mtime = load_due_failures(config.due_measurements) if config.due_measurements else (None, None)
        failures += [Entry(r["when"], f"FAIL: {r['text']}", f"due:{r['id']}") for r in rows or ()]
        sources["due"] = mtime
    return BriefInputs(project=config.slug, as_of=now, cards=cards, decisions=decisions,
                       failures=tuple(failures), sources=sources)

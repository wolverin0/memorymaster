"""Bounded derived continuity receipts; never acknowledge changes at session end."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

SCHEMA = "memorymaster.project-sessions.v1"
MAX_STATE_BYTES = 8 * 1024 * 1024
MAX_BASELINE_BYTES = 128 * 1024
ACTIVE_TTL = timedelta(hours=12)


def _encoded(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_encoded(value)).hexdigest()


def _time(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("invalid timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timezone required")
    return parsed.astimezone(timezone.utc)


def session_state_path(config) -> Path:
    slug = config.scope.removeprefix("project:")
    if not all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", part) for part in (slug, config.context_key)):
        raise ValueError("invalid project identity")
    return Path(config.state_root) / config.context_key / slug / "sessions.json"


def _empty(config) -> dict:
    return {"schema": SCHEMA, "activation_epoch": config.epoch, "scope": config.scope,
            "context_key": config.context_key, "seq": 0, "latest_end": None,
            "active": {}, "completed": [], "baselines": {}}


def read_session_state(config) -> dict:
    """Read one atomic state file without initializing directories or source stores."""
    if not config.enabled:
        return _empty(config)
    path = session_state_path(config)
    if not path.exists():
        return _empty(config)
    with path.open("rb") as handle:
        raw = handle.read(MAX_STATE_BYTES + 1)
    if len(raw) > MAX_STATE_BYTES:
        raise ValueError("state_capacity")
    state = json.loads(raw)
    if not isinstance(state, dict):
        raise ValueError("invalid_state")
    if (state.get("schema") != SCHEMA or state.get("scope") != config.scope
            or state.get("context_key") != config.context_key):
        raise ValueError("state_identity")
    if state.get("activation_epoch") != config.epoch:
        return _empty(config)
    if (type(state.get("seq")) is not int or state["seq"] < 0
            or not isinstance(state.get("active"), dict)
            or not isinstance(state.get("completed"), list)
            or not isinstance(state.get("baselines"), dict)):
        raise ValueError("invalid_state")
    if (len(state["active"]) > 16 or len(state["completed"]) > 4
            or len(state["baselines"]) > 36):
        raise ValueError("state_capacity")
    for key, baseline in state["baselines"].items():
        if not isinstance(baseline, dict) or len(_encoded(baseline)) > MAX_BASELINE_BYTES or _digest(baseline) != key:
            raise ValueError("baseline_integrity")
    for record in [*state["active"].values(), *state["completed"]]:
        if (not isinstance(record, dict) or not isinstance(record.get("session"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", record["session"])
                or type(record.get("seq")) is not int or record["seq"] > state["seq"]):
            raise ValueError("invalid_record")
        _time(record["start_at"])
        _time(record["read_at"])
        if record.get("end_at"):
            if _time(record["end_at"]) < _time(record["start_at"]):
                raise ValueError("invalid_end")
        if record["baseline"] not in state["baselines"]:
            raise ValueError("baseline_unavailable")
        if record.get("prior_baseline") and record["prior_baseline"] not in state["baselines"]:
            raise ValueError("baseline_unavailable")
    latest = state["latest_end"]
    if latest is not None and latest not in state["completed"]:
        raise ValueError("end_pointer_integrity")
    return state


@dataclass(frozen=True)
class SessionDelta:
    text: str
    status: str
    supports: list[dict] = field(default_factory=list)
    omitted: int = 0


def _short(text: str, size: int) -> str:
    raw = " ".join(text.split()).encode("utf-8")
    return raw.decode("utf-8") if len(raw) <= size else raw[:size - 3].decode("utf-8", "ignore") + "..."


def build_session_delta(baseline: dict | None, projection_coverage: dict | None,
                        current: dict, *, diagnostic: str = "") -> SessionDelta:
    """Union start and projection differences, with current authorized supports only."""
    def factual(value):
        return {key: support for key, support in value.items() if key.startswith(("claim:", "card:"))}
    coverage = factual(current.get("coverage", {}))
    items = current.get("items", {})
    comparisons = [factual(value) for value in (baseline, projection_coverage) if value is not None]
    changed = {key for value in comparisons for key in coverage if value.get(key) != coverage[key]}
    removed = {key for value in comparisons for key in value if key not in coverage}
    # A currently authorized closed card may be carried in items outside open-card coverage.
    changed.update(key for key in removed if key in items)
    withdrawn = len(removed - changed)
    complete = bool(current.get("complete")) and projection_coverage is not None
    status = "partial" if not complete else "first_recorded_session" if baseline is None else "ok"
    heading = "Prior-session changes and pending projection changes; current-state comparison."
    if not comparisons:
        status = "partial"
    label = "first recorded session" if status == "first_recorded_session" else status
    if diagnostic:
        label += "; " + diagnostic
    ordered = sorted(changed)
    chosen: list[tuple[str, dict]] = []
    omitted = 0
    for key in ordered:
        item = items.get(key)
        if not item or not item.get("support"):
            omitted += 1
            continue
        support = item["support"]
        reference = f"#{support['id']}" if support.get("kind") == "claim" else str(support["id"])
        chosen.append((f"- {_short(item.get('text', ''), 110)} [{reference}]", support))
    # Always reserve explicit omission/withdrawal counts before selecting lines.
    def render():
        lines = [heading, label, *(line for line, _ in chosen)]
        if not changed and not withdrawn and complete and baseline is not None:
            lines.append("No changes.")
        lines.append(f"withdrawn={withdrawn}; omitted={omitted}")
        return "\n".join(lines)
    while len(render().encode("utf-8")) > 500 and chosen:
        chosen.pop()
        omitted += 1
    text = render()
    if len(text.encode("utf-8")) > 500:
        raise ValueError("delta_budget")
    supports = []
    for line, support in chosen:
        start = text.index(line)
        supports.append({"support": support, "start": start, "end": start + len(line),
                         "display_sha256": hashlib.sha256(line.encode()).hexdigest()})
    return SessionDelta(text, status, supports, omitted)


def _compact(state: dict) -> None:
    state["completed"] = sorted(state["completed"], key=lambda r: (r["end_at"], r["seq"]))[-4:]
    used = set()
    for record in [*state["active"].values(), *state["completed"]]:
        used.add(record["baseline"])
        if record.get("prior_baseline"):
            used.add(record["prior_baseline"])
    state["baselines"] = {key: val for key, val in state["baselines"].items() if key in used}
    if len(state["baselines"]) > 36 or len(_encoded(state)) > MAX_STATE_BYTES:
        raise ValueError("state_capacity")


def _select_prior(state: dict, now: datetime) -> tuple[dict | None, str]:
    latest = state["latest_end"]
    stale = [record for record in state["active"].values()
             if now - _time(record["start_at"]) >= ACTIVE_TTL]
    fallback = max(stale, key=lambda record: record["start_at"], default=None)
    if fallback and (latest is None or (
            now - _time(latest["end_at"]) >= ACTIVE_TTL
            and _time(fallback["start_at"]) > _time(latest["start_at"]))):
        return fallback, "prior_session_unclosed"
    return latest, ""


def record_project_session_start(config, session_id: str, *, projection: dict | None = None,
                                 snapshot_loader: Callable[[], dict] | None = None,
                                 now: datetime | None = None) -> dict:
    if not config.enabled:
        return {"status": "inactive"}
    if not session_id or not config.epoch:
        return {"status": "baseline_unavailable"}
    from memorymaster.profile.project_projection import atomic_json, project_lock

    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    sid = hashlib.sha256(session_id.encode()).hexdigest()
    path = session_state_path(config)
    lock = path.with_suffix(".lock")
    for _ in range(2):
        try:
            with project_lock(lock, timeout=0.05) as acquired:
                if not acquired:
                    return {"status": "continuity_pending"}
                state = read_session_state(config)
                seq = state["seq"]
                duplicate = state["active"].get(sid)
                if any(record["session"] == sid for record in state["completed"]):
                    return {"status": "already_completed"}
                prior, diagnostic = _select_prior(state, now)
                if duplicate:
                    prior_id = duplicate.get("prior_baseline")
                    diagnostic = duplicate.get("diagnostic", "")
                else:
                    prior_id = prior["baseline"] if prior else None
                baseline = copy.deepcopy(state["baselines"].get(prior_id))
            if snapshot_loader is None:
                if projection is None and baseline is None:
                    return {"status": "baseline_unavailable"}
                from memorymaster.profile.project_repository import snapshot_since

                current = snapshot_since(config, projection["source_coverage"] if projection else baseline,
                                         projection["read_at"] if projection else prior["read_at"])
            else:
                current = snapshot_loader()
            if not current.get("complete"):
                return {"status": "baseline_unavailable",
                        "delta": build_session_delta(baseline, None, current)}
            coverage = current["coverage"]
            if len(_encoded(coverage)) > MAX_BASELINE_BYTES:
                return {"status": "baseline_capacity"}
            delta = build_session_delta(baseline, projection.get("source_coverage") if projection else None,
                                        current, diagnostic=diagnostic)
            if duplicate:
                return {"status": "duplicate", "delta": delta}
            with project_lock(lock, timeout=0.05) as acquired:
                if not acquired:
                    return {"status": "continuity_pending"}
                state = read_session_state(config)
                if state["seq"] != seq:
                    continue
                # Stale starts never fabricate ends. Reclaim their slots after retaining the selected baseline.
                state["active"] = {key: val for key, val in state["active"].items()
                                   if now - _time(val["start_at"]) < ACTIVE_TTL}
                if len(state["active"]) >= 16:
                    return {"status": "active_capacity"}
                baseline_id = _digest(coverage)
                state["baselines"][baseline_id] = coverage
                state["seq"] += 1
                state["active"][sid] = {"session": sid, "start_at": now.isoformat(),
                    "read_at": current["read_at"], "baseline": baseline_id,
                    "prior_baseline": prior_id, "prior_receipt": prior.get("session") if prior else None,
                    "diagnostic": diagnostic, "seq": state["seq"]}
                _compact(state)
                atomic_json(path, state)
            return {"status": "ok", "delta": delta}
        except (OSError, ValueError, KeyError, TypeError):
            return {"status": "continuity_pending"}
    return {"status": "continuity_pending"}


def record_project_session_end(config, session_id: str, *, end_at: str | None = None,
                               now: datetime | None = None) -> bool:
    if not config.enabled or not session_id or not session_state_path(config).is_file():
        return False
    from memorymaster.profile.project_projection import atomic_json, project_lock

    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    try:
        end = _time(end_at) if end_at else now
        if end > now:
            return False
        path = session_state_path(config)
        sid = hashlib.sha256(session_id.encode()).hexdigest()
        with project_lock(path.with_suffix(".lock"), timeout=0.05) as acquired:
            if not acquired:
                return False
            state = read_session_state(config)
            record = state["active"].get(sid)
            if record is None or end < _time(record["start_at"]):
                return False
            state["seq"] += 1
            record = {**state["active"].pop(sid), "end_at": end.isoformat(), "seq": state["seq"]}
            # Completed baselines do not recursively retain their own prior baseline.
            record.pop("prior_baseline", None)
            state["completed"].append(record)
            latest = state["latest_end"]
            if latest is None or (end, record["seq"]) > (_time(latest["end_at"]), latest["seq"]):
                state["latest_end"] = record
            _compact(state)
            atomic_json(path, state)
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def last_project_session_end(config) -> dict | None:
    return read_session_state(config)["latest_end"] if config.enabled else None

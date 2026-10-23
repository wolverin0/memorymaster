from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from memorymaster.profile import project_sessions as sessions


NOW = datetime(2026, 10, 22, 9, tzinfo=timezone.utc)


def config(tmp_path):
    return SimpleNamespace(enabled=True, state_root=tmp_path, context_key="fixture",
                           scope="project:sample", epoch="pilot")


def snapshot(**items):
    return {"coverage": {key: item["support"] for key, item in items.items()},
            "items": items, "complete": True, "read_at": NOW.isoformat()}


def claim(text, version=1):
    return {"key": "claim:1", "text": text, "section": "Decisions",
            "support": {"kind": "claim", "id": 1, "version": version, "hash": text}}


@pytest.mark.parametrize("identifiers,texts", [
    (("a@example.com", "b@example.com"), ("Changed visible work",) * 2),
    (("T-" + "x" * 50 + "a", "T-" + "x" * 50 + "b"),
     ("Visible work " * 12 + "a", "Visible work " * 12 + "b")),
], ids=["redacted", "truncated"])
def test_equal_delta_bullets_keep_occurrence_coordinates(identifiers, texts):
    supports = [{"kind": "card", "id": identifier, "hash": str(index)}
                for index, identifier in enumerate(identifiers)]
    current = {"complete": True,
               "coverage": {"card:" + item["id"]: item for item in supports},
               "items": {"card:" + item["id"]: {"text": text, "support": item}
                         for item, text in zip(supports, texts)}}
    delta = sessions.build_session_delta({}, {}, current)
    assert len(delta.supports) == 2
    assert [item["support"] for item in delta.supports] == supports
    spans = [(item["start"], item["end"]) for item in delta.supports]
    assert len(set(spans)) == 2
    assert spans[0][1] < spans[1][0]
    lines = [delta.text[start:end] for start, end in spans]
    assert lines[0] == lines[1]
    assert lines == delta.text.splitlines()[2:4]


def test_session_gap_no_observable_change_lost(tmp_path):
    cfg = config(tmp_path)
    original = snapshot(**{"claim:1": claim("Original decision")})
    projection = {"source_coverage": original["coverage"], "read_at": NOW.isoformat()}
    first = sessions.record_project_session_start(
        cfg, "S", projection=projection, snapshot_loader=lambda: original, now=NOW)
    assert first["delta"].status == "first_recorded_session"
    changed = snapshot(**{"claim:1": claim("Changed decision", 2)})
    assert sessions.record_project_session_end(cfg, "S", now=NOW + timedelta(minutes=20))
    result = sessions.record_project_session_start(
        cfg, "T", projection=projection, snapshot_loader=lambda: changed,
        now=NOW + timedelta(minutes=21))
    assert "Changed decision" in result["delta"].text
    assert result["delta"].supports
    assert "no changes" not in result["delta"].text.lower()
    # A beat rendering that change does not suppress prior-session work.
    new_projection = {"source_coverage": changed["coverage"], "read_at": changed["read_at"]}
    delta = sessions.build_session_delta(original["coverage"], new_projection["source_coverage"], changed)
    assert "Changed decision" in delta.text


@pytest.mark.timeout(180)
def test_session_end_state_is_atomic_bounded_idempotent(tmp_path):
    cfg = config(tmp_path)
    current = snapshot(**{"claim:1": claim("decision")})
    projection = {"source_coverage": current["coverage"], "read_at": NOW.isoformat()}
    # Each start/end pair is two fsynced atomic writes; 600 pairs already exceed every
    # retention bound many times over, and 10,000 pairs blew the timeout on Windows runners.
    last = 599
    for index in range(last + 1):
        now = NOW + timedelta(minutes=index)
        sessions.record_project_session_start(cfg, str(index), projection=projection,
                                              snapshot_loader=lambda: current, now=now)
        assert sessions.record_project_session_end(cfg, str(index), now=now + timedelta(seconds=1))
    path = sessions.session_state_path(cfg)
    state = json.loads(path.read_text(encoding="utf-8"))
    assert len(state["completed"]) == 4
    assert len(state["baselines"]) == 1
    assert not state["active"]
    previous = path.read_bytes()
    assert not sessions.record_project_session_end(cfg, "0", now=NOW + timedelta(days=2))
    assert path.read_bytes() == previous
    assert state["latest_end"]["session"] != str(last)


def test_duplicate_start_does_not_reset_baseline(tmp_path):
    cfg = config(tmp_path)
    original = snapshot(**{"claim:1": claim("original")})
    projection = {"source_coverage": original["coverage"], "read_at": NOW.isoformat()}
    sessions.record_project_session_start(cfg, "S", projection=projection,
                                          snapshot_loader=lambda: original, now=NOW)
    before = sessions.session_state_path(cfg).read_bytes()
    sessions.record_project_session_start(cfg, "S", projection=projection,
        snapshot_loader=lambda: snapshot(**{"claim:1": claim("new", 2)}),
        now=NOW + timedelta(minutes=1))
    assert sessions.session_state_path(cfg).read_bytes() == before


def test_unclosed_session_ttl_fallback_preserves_closures(tmp_path):
    cfg = config(tmp_path)
    card = {"key": "card:T-X", "text": "A visible card", "section": "Open cards",
            "support": {"kind": "card", "id": "T-X", "hash": "open"}}
    original = snapshot(**{"card:T-X": card})
    projection = {"source_coverage": original["coverage"], "read_at": NOW.isoformat()}
    sessions.record_project_session_start(cfg, "killed", projection=projection,
                                          snapshot_loader=lambda: original, now=NOW)
    closed = {**card, "text": "Completed visible card", "closed": True,
              "support": {**card["support"], "hash": "closed"}}
    current = snapshot(**{"card:T-X": closed})
    result = sessions.record_project_session_start(cfg, "next", projection={
        "source_coverage": current["coverage"], "read_at": current["read_at"]},
        snapshot_loader=lambda: current, now=NOW + timedelta(hours=13))
    assert "prior_session_unclosed" in result["delta"].text
    assert "Completed visible card" in result["delta"].text
    assert sessions.last_project_session_end(cfg) is None


def test_delta_cap_withdrawal_and_no_false_empty():
    current = snapshot(**{f"claim:{i}": {**claim("é" * 300), "key": f"claim:{i}"}
                          for i in range(30)})
    delta = sessions.build_session_delta({"claim:private": {"hash": "old-private"}}, {}, current)
    assert len(delta.text.encode("utf-8")) <= 500
    assert "omitted=" in delta.text
    assert "old-private" not in delta.text
    assert "withdrawn=" in delta.text
    assert "private" not in delta.text
    partial = sessions.build_session_delta({}, None, snapshot())
    assert partial.status == "partial"
    assert "no changes" not in partial.text.lower()


def test_delta_stops_loading_after_unavailable_support():
    current = snapshot(**{f"claim:{number}": {**claim("changed"), "key": f"claim:{number}"}
                          for number in range(1, 41)})
    current["items"] = {}
    loaded = []

    def unavailable(key):
        loaded.append(key)
        return None

    delta = sessions.build_session_delta({}, {}, current, item_loader=unavailable)
    assert len(loaded) == 1
    assert not delta.supports
    assert delta.omitted == 40
    assert "No changes" not in delta.text


def test_disabled_state_never_reads_or_writes(tmp_path):
    cfg = config(tmp_path)
    cfg.enabled = False
    assert sessions.record_project_session_start(cfg, "S")["status"] == "inactive"
    assert not sessions.record_project_session_end(cfg, "S")
    assert not list(tmp_path.iterdir())


def test_missing_projection_keeps_completed_baseline_comparison(tmp_path, monkeypatch):
    from memorymaster.profile import project_repository

    cfg = config(tmp_path)
    original = snapshot(**{"claim:1": claim("original")})
    projection = {"source_coverage": original["coverage"], "read_at": NOW.isoformat()}
    sessions.record_project_session_start(cfg, "S", projection=projection,
                                          snapshot_loader=lambda: original, now=NOW)
    sessions.record_project_session_end(cfg, "S", now=NOW + timedelta(minutes=1))
    monkeypatch.setattr(project_repository, "snapshot_since", lambda config, coverage, read_at:
                        snapshot(**{"claim:1": claim("changed without projection", 2)}))
    result = sessions.record_project_session_start(cfg, "T", now=NOW + timedelta(minutes=2))
    assert "changed without projection" in result["delta"].text
    assert result["delta"].status == "partial"


def test_dotted_project_slug_has_bounded_state(tmp_path):
    cfg = config(tmp_path)
    cfg.scope = "project:my.repo"
    assert sessions.session_state_path(cfg).parent.name == "my.repo"


def test_malformed_state_and_end_time_fail_closed(tmp_path):
    cfg = config(tmp_path)
    path = sessions.session_state_path(cfg)
    path.parent.mkdir(parents=True)
    for invalid in ([], None, {"schema": sessions.SCHEMA, "scope": cfg.scope,
                              "context_key": cfg.context_key, "activation_epoch": cfg.epoch,
                              "active": [], "completed": [], "baselines": {}, "seq": 0}):
        path.write_text(json.dumps(invalid))
        assert sessions.record_project_session_start(cfg, "S")["status"] == "continuity_pending"
        assert not sessions.record_project_session_end(cfg, "S", end_at=123)


def test_future_end_is_rejected_and_start_remains(tmp_path):
    cfg = config(tmp_path)
    current = snapshot()
    sessions.record_project_session_start(cfg, "S", projection={"source_coverage": {}, "read_at": NOW.isoformat()},
                                          snapshot_loader=lambda: current, now=NOW)
    assert not sessions.record_project_session_end(cfg, "S", end_at=(NOW + timedelta(seconds=30)).isoformat(), now=NOW)
    assert sessions.last_project_session_end(cfg) is None


def test_internal_scan_fence_never_becomes_delta_support_or_omission():
    current = snapshot()
    current["coverage"]["__scan_fence__"] = {"watched_ids": [42], "max_seen_id": 42}
    delta = sessions.build_session_delta({}, {}, current)
    assert delta.omitted == 0
    assert not delta.supports
    assert "No changes" in delta.text


def test_out_of_order_ends_capacity_epoch_and_interrupted_replace(tmp_path, monkeypatch):
    from memorymaster.profile import project_projection

    cfg = config(tmp_path)
    current = snapshot()
    projection = {"source_coverage": {}, "read_at": NOW.isoformat()}
    def start(sid, minute=0):
        return sessions.record_project_session_start(cfg, sid, projection=projection,
            snapshot_loader=lambda: current, now=NOW + timedelta(minutes=minute))
    start("old")
    start("latest", 1)
    assert sessions.record_project_session_end(cfg, "latest", now=NOW + timedelta(minutes=5))
    assert sessions.record_project_session_end(cfg, "old", now=NOW + timedelta(minutes=3))
    latest = sessions.last_project_session_end(cfg)
    assert latest["end_at"] == (NOW + timedelta(minutes=5)).isoformat()
    for index in range(16):
        assert start(f"active-{index}", 6)["status"] == "ok"
    assert start("overflow", 6)["status"] == "active_capacity"
    assert sessions.last_project_session_end(cfg) == latest
    path = sessions.session_state_path(cfg)
    before = path.read_bytes()
    with project_projection.project_lock(path.with_suffix(".lock")) as acquired:
        assert acquired
        assert start("locked", 6)["status"] == "continuity_pending"
    def fail_replace(*args):
        raise OSError("interrupted replace")
    monkeypatch.setattr(project_projection, "atomic_json", fail_replace)
    assert not sessions.record_project_session_end(cfg, "active-0", now=NOW + timedelta(minutes=7))
    assert path.read_bytes() == before
    cfg.epoch = "new-activation"
    assert sessions.last_project_session_end(cfg) is None


def test_stale_active_start_cannot_override_a_recent_explicit_end(tmp_path):
    cfg = config(tmp_path)
    old = snapshot(**{"claim:1": claim("before overlapping work")})
    changed = snapshot(**{"claim:1": claim("retained overlapping work", 2)})
    projection = {"source_coverage": old["coverage"], "read_at": NOW.isoformat()}
    sessions.record_project_session_start(cfg, "completed-later", projection=projection,
        snapshot_loader=lambda: old, now=NOW)
    sessions.record_project_session_start(cfg, "crashed-overlap", projection=projection,
        snapshot_loader=lambda: changed, now=NOW + timedelta(hours=6))
    sessions.record_project_session_end(cfg, "completed-later", now=NOW + timedelta(hours=20))
    result = sessions.record_project_session_start(cfg, "next", projection={
        "source_coverage": changed["coverage"], "read_at": changed["read_at"]},
        snapshot_loader=lambda: changed, now=NOW + timedelta(hours=20, minutes=1))
    assert "retained overlapping work" in result["delta"].text
    assert "prior_session_unclosed" not in result["delta"].text


def test_unclosed_session_before_ttl_retains_closed_card_after_refresh(tmp_path):
    cfg = config(tmp_path)
    support = {"kind": "card", "id": "T-1", "hash": "open", "state": "ready"}
    opened = {"key": "card:T-1", "support": support, "text": "T-1 [ready] Visible card", "open": True}
    original = {"coverage": {"card:T-1": support}, "items": {"card:T-1": opened},
                "complete": True, "read_at": NOW.isoformat()}
    projection = {"source_coverage": original["coverage"], "read_at": original["read_at"]}
    assert sessions.record_project_session_start(cfg, "S", projection=projection,
        snapshot_loader=lambda: original, now=NOW)["status"] == "ok"
    closed = {**opened, "support": {**support, "hash": "closed", "state": "done"},
              "text": "T-1 [done] Visible card", "open": False}
    refreshed = {"coverage": {}, "items": {"card:T-1": closed}, "complete": True,
                 "read_at": (NOW + timedelta(minutes=9)).isoformat()}
    result = sessions.record_project_session_start(cfg, "T", projection={
        "source_coverage": {}, "read_at": refreshed["read_at"]},
        snapshot_loader=lambda: refreshed, now=NOW + timedelta(minutes=10))
    delta = result["delta"]
    assert delta.supports and delta.supports[0]["support"] == closed["support"]
    assert "T-1 [done] Visible card" in delta.text
    assert "prior_session_unclosed" in delta.text and "overlap_unresolved" in delta.text
    assert delta.status == "partial"
    assert "first recorded session" not in delta.text and "No changes" not in delta.text
    assert sessions.last_project_session_end(cfg) is None
    # A start alone cannot establish whether its predecessor crashed or is
    # overlapping. Keep both live receipts; a later genuine end still works.
    state = sessions.read_session_state(cfg)
    assert len(state["active"]) == 2 and not state["completed"]
    again = sessions.record_project_session_start(cfg, "U", projection={
        "source_coverage": {}, "read_at": refreshed["read_at"]},
        snapshot_loader=lambda: refreshed, now=NOW + timedelta(minutes=10, seconds=30))
    assert again["delta"].status == "partial"
    assert again["delta"].supports[0]["support"] == closed["support"]
    assert sessions.record_project_session_end(cfg, "S", now=NOW + timedelta(minutes=11))
    latest = sessions.record_project_session_start(cfg, "V", projection={
        "source_coverage": {}, "read_at": refreshed["read_at"]},
        snapshot_loader=lambda: refreshed, now=NOW + timedelta(minutes=12))
    assert latest["delta"].status == "ok"
    assert "overlap_unresolved" not in latest["delta"].text

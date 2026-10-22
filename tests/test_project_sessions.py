from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

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


def test_session_end_state_is_atomic_bounded_idempotent(tmp_path):
    cfg = config(tmp_path)
    current = snapshot(**{"claim:1": claim("decision")})
    projection = {"source_coverage": current["coverage"], "read_at": NOW.isoformat()}
    for index in range(100):
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
    assert "99" not in state["latest_end"]["session"]


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

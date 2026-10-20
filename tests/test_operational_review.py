from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from memorymaster.operations import operational_review as review


def test_default_retrieval_keeps_exact_rows_then_fills_without_accesses(tmp_path, monkeypatch):
    query = "deployment worker retry queue timeout"
    db = _db(tmp_path / "memory.db")
    rows = [
        {"claim": SimpleNamespace(id=n, human_id=f"mm-{n}")}
        for n in range(1, 8)
    ]
    service = MagicMock()
    # Expansion ranks partial matches before exact ones and exceeds the cap.
    service.query_rows.side_effect = [rows[:2], [rows[2], rows[0], *rows[3:]]]
    service_class = MagicMock(return_value=service)
    monkeypatch.setattr("memorymaster.core.service.MemoryService", service_class)

    assert review._default_retrieval(db, query) == [f"mm-{n}" for n in range(1, 6)]

    service_class.assert_called_once_with(str(db), workspace_root=db.parent, read_only=True)
    calls = [call.kwargs for call in service.query_rows.call_args_list]
    assert [call["query_text"] for call in calls] == [query, " OR ".join(query.split())]
    for call in calls:
        assert call["limit"] == 5
        assert call["record_accesses"] is False
        assert call["include_stale"] is False
        assert call["include_conflicted"] is False
        assert call["include_candidates"] is False
        assert call["allow_sensitive"] is False
    service._record_accesses.assert_not_called()
    service.store.record_accesses_batch.assert_not_called()
    service.store.record_access.assert_not_called()


def _db(path: Path) -> Path:
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE schema_versions(version INTEGER);
        INSERT INTO schema_versions VALUES (21);
        CREATE TABLE graph_observation_jobs(
            stage TEXT, status TEXT, lease_expires_at TEXT, outcome TEXT
        );
        INSERT INTO graph_observation_jobs
            VALUES ('discover', 'completed', NULL, 'components_found');
        CREATE TABLE graph_observations(observation_claim_id INTEGER);
        CREATE TABLE graph_observation_supports(
            observation_claim_id INTEGER, supporting_claim_id INTEGER,
            evidence_item_id INTEGER, source_item_id INTEGER
        );
        CREATE TABLE entity_edge_supports(supporting_claim_id INTEGER);
        CREATE TABLE claim_evidence_links(claim_id INTEGER, evidence_item_id INTEGER);
        CREATE TABLE evidence_items(id INTEGER, source_item_id INTEGER, sensitivity TEXT);
        CREATE TABLE source_items(id INTEGER, sensitivity TEXT, retired_at TEXT);
        CREATE TABLE compiled_profile_runs(status TEXT);
        INSERT INTO compiled_profile_runs VALUES ('completed');
        CREATE TABLE compiled_profile_facts(
            id INTEGER PRIMARY KEY, status TEXT, support_count INTEGER, independent_sessions INTEGER
        );
        INSERT INTO compiled_profile_facts VALUES (1, 'active', 2, 2);
        CREATE TABLE compiled_profile_supports(fact_id INTEGER, session_id TEXT, supported_at TEXT);
        INSERT INTO compiled_profile_supports VALUES (1, 'a', datetime('now')), (1, 'b', datetime('now'));
        CREATE TABLE claims(
            id INTEGER PRIMARY KEY, human_id TEXT, text TEXT, claim_type TEXT,
            scope TEXT, tenant_id TEXT, status TEXT, confidence REAL,
            subject TEXT, predicate TEXT, object_value TEXT, created_at TEXT
        );
        INSERT INTO claims VALUES (
            1, 'mm-safe', 'Uses a repo-relative config file', 'fact',
            'project:test', NULL, 'confirmed', 0.9, 'MemoryMaster', 'uses',
            'scripts/config.json', datetime('now')
        );
    """)
    connection.commit()
    connection.close()
    return path


def test_clean_review_checks_pass(tmp_path: Path, monkeypatch) -> None:
    config = review.ReviewConfig(
        db=_db(tmp_path / "memory.db"),
        expected_version="4.7.2",
        canary_query="why",
        canary_human_id="mm-target",
    )
    monkeypatch.setattr(review.importlib.metadata, "version", lambda _name: "4.7.2")
    monkeypatch.setenv("MEMORYMASTER_GRAPH_OBSERVATIONS", "1")
    monkeypatch.setenv("MEMORYMASTER_COMPILED_PROFILE", "1")

    results = [
        review.check_runtime(config),
        review.check_database(config),
        review.check_feature_activation(config),
        review.check_graph_observations(config),
        review.check_compiled_profile(config),
        review.check_recent_private_context(config),
        review.check_retrieval(config, retrieve=lambda *_: ["mm-target"]),
        # AO-2 beat (T-1640): inactive, it must not move the verdict of a clean review.
        review.check_beat_health(config),
    ]

    assert review.exit_code(results) == 0
    assert {item.verdict for item in results} == {review.Verdict.PASS}
    assert results[-1].name == "beat_health" and "inactive" in results[-1].detail
    # T-1662: the snapshot/slot checks are the last two registered checks and pass on
    # hermetic fixtures (a fresh snapshot, four job_start events per job).
    assert [check.__name__ for check in review.REVIEW_CHECKS[-2:]] == ["check_snapshot_dir", "check_scheduled_slots"]
    now = datetime.now(timezone.utc)
    snapshots = tmp_path / "snapshots"
    snapshots.mkdir()
    (snapshots / "db.sqlite").write_bytes(b"snapshot")
    logs = {}
    for mode in ("dream", "steward"):
        logs[mode] = tmp_path / f"{mode}.log"
        logs[mode].write_text("".join(json.dumps({"event": "job_start", "job": mode,
                                                   "ts": (now - timedelta(hours=h)).isoformat()}) + "\n"
                                      for h in (1, 7, 13, 19)), encoding="utf-8")
    hermetic = review.ReviewConfig(db=config.db, snapshot_dir=snapshots,
                                   dream_log=logs["dream"], steward_log=logs["steward"])
    assert review.check_snapshot_dir(hermetic, now=now).verdict is review.Verdict.PASS
    assert review.check_scheduled_slots(hermetic, now=now).verdict is review.Verdict.PASS


def test_snapshot_dir_fresh_stale_and_unreachable(tmp_path: Path, monkeypatch) -> None:
    now = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
    folder = tmp_path / "snapshots"
    folder.mkdir()
    snapshot = folder / "db.sqlite"
    snapshot.write_bytes(b"snapshot")
    monkeypatch.setattr(review, "_utc", lambda value=None: now if value is None else value.astimezone(timezone.utc))
    import os
    os.utime(snapshot, (now.timestamp() - 3600, now.timestamp() - 3600))
    config = review.ReviewConfig(db=tmp_path / "db", snapshot_dir=folder)
    assert review.check_snapshot_dir(config, now=now).verdict is review.Verdict.PASS
    os.utime(snapshot, (now.timestamp() - 9 * 86400, now.timestamp() - 9 * 86400))
    assert review.check_snapshot_dir(config, now=now).verdict is review.Verdict.WARN
    assert review.check_snapshot_dir(review.ReviewConfig(db=config.db, snapshot_dir=tmp_path / "absent"), now=now).verdict is review.Verdict.FAIL
    empty = tmp_path / "empty"
    empty.mkdir()
    assert review.check_snapshot_dir(review.ReviewConfig(db=config.db, snapshot_dir=empty), now=now).verdict is review.Verdict.WARN
    # A bad max-age override falls back to the default instead of crashing the review.
    monkeypatch.setenv("MEMORYMASTER_SNAPSHOT_MAX_AGE_DAYS", "soon")
    assert review.check_snapshot_dir(config, now=now).verdict is review.Verdict.WARN


def test_scheduled_slots_complete_and_missing(tmp_path: Path) -> None:
    now = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
    logs = {}
    for mode in ("dream", "steward"):
        path = tmp_path / f"{mode}.log"
        lines = []
        for hours in (1, 7, 13, 19):
            stamp = (now - timedelta(hours=hours)).isoformat()
            lines.append(json.dumps({"event": "job_start", "job": mode, "ts": stamp}))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        logs[mode] = path
    config = review.ReviewConfig(db=tmp_path / "db", dream_log=logs["dream"], steward_log=logs["steward"])
    assert review.check_scheduled_slots(config, now=now).verdict is review.Verdict.PASS
    # The review rarely runs on a 6-hour boundary: slots are relative to ``now``, so a
    # window ending at 13:47 still sees exactly four slots.
    assert review.check_scheduled_slots(config, now=now + timedelta(hours=1, minutes=47)).verdict is review.Verdict.PASS
    # One missing slot (a run skipped at 13 h ago) is a WARN; two runs in the same slot do not hide it.
    lines = [json.dumps({"event": "job_start", "job": "dream", "ts": (now - timedelta(hours=h)).isoformat()})
             for h in (1, 2, 7, 19)]
    logs["dream"].write_text("\n".join(lines) + "\nnot json\n[1, 2]\n", encoding="utf-8")
    result = review.check_scheduled_slots(config, now=now)
    assert result.verdict is review.Verdict.WARN and result.counts["missing_slots"] == 1
    logs["dream"].write_text("", encoding="utf-8")
    assert review.check_scheduled_slots(config, now=now).verdict is review.Verdict.FAIL


def test_private_context_and_blocked_job_fail(tmp_path: Path, monkeypatch) -> None:
    # HERMETICO A PROPOSITO: estos casos ejercitan el subsistema PRENDIDO.
    # Sin fijar la variable heredaban el entorno del que corria los tests, y
    # el 2026-08-29 eso me engano: pasaban local (mi shell tenia =1) y
    # fallaban en CI (sin variable => deshabilitado => el check sale temprano).
    monkeypatch.setenv("MEMORYMASTER_GRAPH_OBSERVATIONS", "1")
    db = _db(tmp_path / "memory.db")
    connection = sqlite3.connect(db)
    connection.execute(
        "INSERT INTO graph_observation_jobs VALUES ('synthesize', 'blocked', NULL, NULL)"
    )
    connection.execute(
        "INSERT INTO claims(id, human_id, text, claim_type, scope, status, confidence, "
        "subject, predicate, object_value, created_at) "
        "VALUES (2, ?, ?, 'fact', 'project:test', 'confirmed', 0.9, '', '', '', datetime('now'))",
        ("mm-private", "The service uses 10.1.2.3 internally"),
    )
    connection.commit()
    connection.close()
    config = review.ReviewConfig(db=db)

    graph = review.check_graph_observations(config)
    intake = review.check_recent_private_context(config)

    assert graph.verdict is review.Verdict.FAIL
    assert intake.verdict is review.Verdict.FAIL
    assert intake.human_ids == ("mm-private",)
    assert review.exit_code([graph, intake]) == 1


def test_unknown_graph_support_sensitivity_fails(tmp_path: Path, monkeypatch) -> None:
    # HERMETICO A PROPOSITO: estos casos ejercitan el subsistema PRENDIDO.
    # Sin fijar la variable heredaban el entorno del que corria los tests, y
    # el 2026-08-29 eso me engano: pasaban local (mi shell tenia =1) y
    # fallaban en CI (sin variable => deshabilitado => el check sale temprano).
    monkeypatch.setenv("MEMORYMASTER_GRAPH_OBSERVATIONS", "1")
    db = _db(tmp_path / "memory.db")
    connection = sqlite3.connect(db)
    connection.execute("INSERT INTO entity_edge_supports VALUES (1)")
    connection.execute("INSERT INTO claim_evidence_links VALUES (1, 1)")
    connection.execute("INSERT INTO evidence_items VALUES (1, 1, NULL)")
    connection.execute("INSERT INTO source_items(id, sensitivity) VALUES (1, NULL)")
    connection.commit()
    connection.close()

    graph = review.check_graph_observations(review.ReviewConfig(db=db))

    assert graph.verdict is review.Verdict.FAIL
    assert graph.counts is not None
    assert graph.counts["unknown_sensitivity_rows"] == 1


def test_ineligible_confirmed_observation_fails_review(tmp_path: Path, monkeypatch) -> None:
    # HERMETICO A PROPOSITO: estos casos ejercitan el subsistema PRENDIDO.
    # Sin fijar la variable heredaban el entorno del que corria los tests, y
    # el 2026-08-29 eso me engano: pasaban local (mi shell tenia =1) y
    # fallaban en CI (sin variable => deshabilitado => el check sale temprano).
    monkeypatch.setenv("MEMORYMASTER_GRAPH_OBSERVATIONS", "1")
    db = _db(tmp_path / "memory.db")
    connection = sqlite3.connect(db)
    connection.execute("INSERT INTO graph_observations VALUES (20)")
    connection.execute(
        "INSERT INTO claims VALUES "
        "(20, 'mm-observation', 'Derived summary', 'observation', 'project:test', NULL, "
        "'confirmed', 0.9, '', '', '', datetime('now'))"
    )
    for claim_id, confidence in ((10, 0.8), (11, 0.64), (12, 0.9)):
        connection.execute(
            "INSERT INTO claims VALUES (?, ?, 'support', 'fact', 'project:test', NULL, "
            "'confirmed', ?, '', '', '', datetime('now'))",
            (claim_id, f"mm-{claim_id}", confidence),
        )
    connection.executemany(
        "INSERT INTO source_items(id, sensitivity) VALUES (?, 'none')",
        ((200,), (201,)),
    )
    connection.executemany(
        "INSERT INTO evidence_items VALUES (?, ?, 'none')",
        ((100, 200), (101, 201)),
    )
    connection.executemany(
        "INSERT INTO graph_observation_supports VALUES (20, ?, ?, ?)",
        ((10, 100, 200), (11, 101, 201), (12, 100, 200)),
    )
    connection.commit()
    connection.close()

    graph = review.check_graph_observations(review.ReviewConfig(db=db))

    assert graph.verdict is review.Verdict.FAIL
    assert graph.counts is not None
    assert graph.counts["ineligible_confirmed_observations"] == 1


def test_missing_canary_and_disabled_features_warn(tmp_path: Path, monkeypatch) -> None:
    config = review.ReviewConfig(db=_db(tmp_path / "memory.db"))
    monkeypatch.delenv("MEMORYMASTER_GRAPH_OBSERVATIONS", raising=False)
    monkeypatch.delenv("MEMORYMASTER_COMPILED_PROFILE", raising=False)

    results = [review.check_feature_activation(config), review.check_retrieval(config)]

    assert review.exit_code(results) == 3


def test_review_process_flag_cannot_hide_retained_worker_failures(tmp_path, monkeypatch):
    monkeypatch.delenv("MEMORYMASTER_GRAPH_OBSERVATIONS", raising=False)
    db = _db(tmp_path / "memory.db")
    with sqlite3.connect(db) as connection:
        connection.execute(
            "INSERT INTO graph_observation_jobs VALUES ('synthesize', 'blocked', NULL, NULL)"
        )
    result = review.check_graph_observations(review.ReviewConfig(db=db))
    assert result.verdict is review.Verdict.WARN
    assert result.counts["blocked"] == 1
    assert result.counts["review_process_enabled"] == 0
    assert "worker activation unverified" in result.detail


def test_powershell_scheduler_contract_is_bounded() -> None:
    root = Path(__file__).resolve().parents[1]
    installer = (root / "scripts" / "install-windows-operational-review.ps1").read_text(encoding="utf-8")
    runner = (root / "scripts" / "windows-operational-review.ps1").read_text(encoding="utf-8")

    assert "RepetitionInterval" in installer
    assert "ExecutionTimeLimit" in installer
    assert "New-TimeSpan -Minutes 25" in installer
    assert "MultipleInstances IgnoreNew" in installer
    assert "memorymaster.operations.review_supervisor" in runner
    assert "every_hours = $EveryHours" in installer
    assert "work-receipt" not in runner.lower()


def test_review_reports_why_completed_discovery_found_nothing(tmp_path: Path, monkeypatch) -> None:
    """3,146 completed discovery jobs read as success; only the split says otherwise."""
    # HERMETICO A PROPOSITO: estos casos ejercitan el subsistema PRENDIDO.
    # Sin fijar la variable heredaban el entorno del que corria los tests, y
    # el 2026-08-29 eso me engano: pasaban local (mi shell tenia =1) y
    # fallaban en CI (sin variable => deshabilitado => el check sale temprano).
    monkeypatch.setenv("MEMORYMASTER_GRAPH_OBSERVATIONS", "1")
    db = _db(tmp_path / "memory.db")
    connection = sqlite3.connect(db)
    connection.executemany(
        "INSERT INTO graph_observation_jobs VALUES ('discover', 'completed', NULL, ?)",
        [("no_supports",), ("no_supports",), ("no_components",)],
    )
    connection.commit()
    connection.close()

    graph = review.check_graph_observations(review.ReviewConfig(db=db))

    assert graph.counts["completed_discovery"] == 4
    assert graph.counts["discovery_no_supports"] == 2
    assert graph.counts["discovery_no_components"] == 1
    assert graph.counts["discovery_components_found"] == 1


def _profile_with_two_active_facts(tmp_path: Path, monkeypatch, rendered: list[int]) -> review.ReviewConfig:
    db = _db(tmp_path / "memory.db")
    with sqlite3.connect(db) as connection:
        connection.execute("INSERT INTO compiled_profile_facts VALUES (2, 'active', 2, 2)")
        connection.execute(
            "INSERT INTO compiled_profile_supports VALUES (2, 'a', datetime('now')), (2, 'b', datetime('now'))"
        )
    projections = tmp_path / "projections"
    projections.mkdir()
    (projections / "user-profile.json").write_text(
        json.dumps({"facts": [{"fact_id": fact_id} for fact_id in rendered]}), encoding="utf-8"
    )
    monkeypatch.setenv("MEMORYMASTER_PROFILE_OUTPUT_DIR", str(projections))
    monkeypatch.setenv("MEMORYMASTER_COMPILED_PROFILE", "1")
    return review.ReviewConfig(db=db)


def test_profile_warns_when_the_budget_leaves_active_facts_out(tmp_path: Path, monkeypatch) -> None:
    # 2026-10-02 live: 61 active facts, 52 injected at 1399/1400 tokens, and the
    # check stayed silent about the 9 standing constraints the renderer cut.
    result = review.check_compiled_profile(_profile_with_two_active_facts(tmp_path, monkeypatch, [1]))
    assert result.verdict is review.Verdict.WARN
    assert "1 of 2 active facts are not in the injected profile" in result.detail
    assert result.counts["rendered_facts"] == 1 and result.counts["omitted_active_facts"] == 1


def test_profile_with_every_active_fact_injected_passes(tmp_path: Path, monkeypatch) -> None:
    result = review.check_compiled_profile(_profile_with_two_active_facts(tmp_path, monkeypatch, [1, 2]))
    assert result.verdict is review.Verdict.PASS
    assert result.counts["omitted_active_facts"] == 0

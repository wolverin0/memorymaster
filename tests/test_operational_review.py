from __future__ import annotations

import sqlite3
from pathlib import Path

from memorymaster.operations import operational_review as review


def _db(path: Path) -> Path:
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE schema_versions(version INTEGER);
        INSERT INTO schema_versions VALUES (21);
        CREATE TABLE graph_observation_jobs(
            stage TEXT, status TEXT, lease_expires_at TEXT
        );
        INSERT INTO graph_observation_jobs VALUES ('discover', 'completed', NULL);
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
        CREATE TABLE compiled_profile_supports(fact_id INTEGER, session_id TEXT);
        INSERT INTO compiled_profile_supports VALUES (1, 'a'), (1, 'b');
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
    ]

    assert review.exit_code(results) == 0
    assert {item.verdict for item in results} == {review.Verdict.PASS}


def test_private_context_and_blocked_job_fail(tmp_path: Path) -> None:
    db = _db(tmp_path / "memory.db")
    connection = sqlite3.connect(db)
    connection.execute("INSERT INTO graph_observation_jobs VALUES ('synthesize', 'blocked', NULL)")
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


def test_unknown_graph_support_sensitivity_fails(tmp_path: Path) -> None:
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


def test_ineligible_confirmed_observation_fails_review(tmp_path: Path) -> None:
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


def test_powershell_scheduler_contract_is_bounded() -> None:
    root = Path(__file__).resolve().parents[1]
    installer = (root / "scripts" / "install-windows-operational-review.ps1").read_text(encoding="utf-8")
    runner = (root / "scripts" / "windows-operational-review.ps1").read_text(encoding="utf-8")

    assert "RepetitionInterval" in installer
    assert "ExecutionTimeLimit" in installer
    assert "New-TimeSpan -Minutes 15" in installer
    assert "MultipleInstances IgnoreNew" in installer
    assert "review_performed = $true" in runner
    assert "work-receipt" not in runner.lower()

"""Ledger reader at real scale (T-2278): ~2.3k one-card files, selection by state and mtime.

Synthetic tmp ledgers only; the real _intel/tasks is never read or written.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
import time

import pytest
from test_project_repository import project_config  # noqa: F401

from memorymaster.profile import project_repository as repository
from memorymaster.profile import project_runtime
from memorymaster.profile.project_repository import OPEN_STATES, load_ledger

DAY = 86_400
STATES = ("done",) * 6 + ("cancelled", "cancelled", "ready", "blocked", "done", "done")  # ~17% open


def _write_card(root, index, state, age_days, repo="demo", padding=0):
    card = {"id": f"T-{index:04d}", "repo": repo, "state": state, "title": f"Card {index}"}
    if padding:
        card["goal"] = "x" * padding
    path = root / f"T-{index:04d}.json"
    path.write_text(json.dumps(card))
    stamp = time.time() - age_days * DAY
    os.utime(path, (stamp, stamp))
    return card


@pytest.fixture(scope="module")
def _big_ledger_dir(tmp_path_factory):
    """2,300 cards written once per module: every 7th is another repo; one in six touched 2 days ago, the rest 90."""
    root = tmp_path_factory.mktemp("big-ledger")
    expected_open, expected_recent_closed, skipped_old_closed = set(), set(), set()
    for index in range(2300):
        state = STATES[index % len(STATES)]
        age = 2 if index % 6 == 0 else 90
        repo = "other" if index % 7 == 0 else "demo"
        _write_card(root, index, state, age, repo=repo, padding=1500)
        if repo != "demo":
            continue
        card_id = f"T-{index:04d}"
        if state in OPEN_STATES:
            expected_open.add(card_id)
        elif age == 2:
            expected_recent_closed.add(card_id)
        else:
            skipped_old_closed.add(card_id)
    return root, expected_open, expected_recent_closed, skipped_old_closed


@pytest.fixture
def big_ledger(project_config, _big_ledger_dir):  # noqa: F811
    root, expected_open, expected_recent_closed, skipped_old_closed = _big_ledger_dir
    return replace(project_config, ledger_root=root), expected_open, expected_recent_closed, skipped_old_closed


def test_selection_keeps_open_and_recent_and_skips_old_closed(big_ledger):
    config, expected_open, recent_closed, old_closed = big_ledger
    load_ledger(config)  # warm the OS file cache: the card files were written moments ago
    start = time.perf_counter()
    cards, complete, diagnostics = load_ledger(config)
    elapsed = time.perf_counter() - start
    assert complete and diagnostics == []
    ids = {card["support"]["id"] for card in cards.values()}
    assert ids == expected_open | recent_closed
    assert not ids & old_closed
    # Open cards survive regardless of age (some are 90 days old); closed ones only when recent.
    assert all(cards[i]["open"] for i in expected_open)
    assert all(not cards[i]["open"] for i in recent_closed)
    assert elapsed < 1.0, f"loading 2300 cards took {elapsed:.2f}s"


def test_byte_cap_applies_to_kept_set_not_whole_directory(big_ledger):
    config, expected_open, recent_closed, _ = big_ledger
    total = sum(path.stat().st_size for path in config.ledger_root.glob("*.json"))
    kept_bytes = sum(
        (config.ledger_root / f"{card_id}.json").stat().st_size for card_id in expected_open | recent_closed
    )
    assert kept_bytes < 0.8 * total  # the directory is bigger than what is kept
    cards, complete, _ = load_ledger(config, max_bytes=kept_bytes)
    assert complete and len(cards) == len(expected_open | recent_closed)
    cards, complete, diagnostics = load_ledger(config, max_bytes=kept_bytes - 1)
    assert (cards, complete, diagnostics) == ({}, False, ["ledger_unavailable"])


def test_malformed_files_are_skipped_and_counted(big_ledger):
    config, expected_open, recent_closed, _ = big_ledger
    root = config.ledger_root
    (root / "broken-1.json").write_text("{")
    (root / "broken-2.json").write_bytes(b"\xff\xfe\x00not json")
    (root / "broken-3.json").write_text(json.dumps({"tasks": "not a list"}))
    cards, complete, diagnostics = load_ledger(config)
    assert not complete
    assert "ledger_malformed:3" in diagnostics and "ledger_incomplete" in diagnostics
    assert {card["support"]["id"] for card in cards.values()} == expected_open | recent_closed


def test_only_malformed_files_is_unavailable_not_a_crash(project_config):  # noqa: F811
    root = project_config.ledger_root
    for stale in root.glob("*.json"):
        stale.unlink()
    (root / "a.json").write_text("{")
    cards, complete, diagnostics = load_ledger(project_config)
    assert cards == {} and not complete
    assert diagnostics == ["ledger_unavailable", "ledger_malformed:1"]


def test_deeply_nested_file_is_counted_as_malformed_not_a_crash(project_config):  # noqa: F811
    root = project_config.ledger_root
    for stale in root.glob("*.json"):
        stale.unlink()
    _write_card(root, 1, "ready", 1)
    (root / "deep.json").write_text("[" * 100_000)
    cards, complete, diagnostics = load_ledger(project_config)
    assert set(cards) == {"T-0001"}
    assert not complete
    assert "ledger_malformed:1" in diagnostics and "ledger_incomplete" in diagnostics


def test_file_cap_is_4096_and_fails_closed(project_config):  # noqa: F811
    root = project_config.ledger_root
    for stale in root.glob("*.json"):
        stale.unlink()
    for index in range(repository.LEDGER_MAX_FILES + 1):
        (root / f"c{index}.json").write_text("{}")
    assert load_ledger(project_config) == ({}, False, ["ledger_unavailable"])
    (root / "c0.json").unlink()
    cards, _, diagnostics = load_ledger(project_config)
    assert (cards, diagnostics) == ({}, [])  # exactly 4096 is accepted


def test_reader_is_read_only(big_ledger):
    config, *_ = big_ledger
    snapshot = {p.name: (p.stat().st_mtime_ns, p.stat().st_size) for p in config.ledger_root.glob("*")}
    load_ledger(config)
    assert snapshot == {p.name: (p.stat().st_mtime_ns, p.stat().st_size) for p in config.ledger_root.glob("*")}


def test_runtime_fingerprint_accepts_the_full_ledger(big_ledger):
    config, *_ = big_ledger
    fingerprint = project_runtime.cheap_fingerprint(config)
    assert len(fingerprint) >= 2300  # one stat entry per card file


def test_project_connection_cache_holds_the_four_project_matrix(tmp_path, project_config):  # noqa: F811
    assert project_runtime.MAX_PROJECTS == 8
    project_runtime._connections.clear()
    try:
        for index in range(5):
            db = tmp_path / f"p{index}.sqlite"
            db.write_bytes(project_config.db_path.read_bytes())
            project_runtime.cheap_fingerprint(replace(project_config, db_path=db))
        assert len(project_runtime._connections) == 5
    finally:
        for connection in project_runtime._connections.values():
            connection.close()
        project_runtime._connections.clear()


@pytest.mark.parametrize(("count", "accepted"), [(8, True), (9, False)])
def test_allowlist_accepts_up_to_eight_projects(tmp_path, monkeypatch, count, accepted):
    monkeypatch.setenv("MEMORYMASTER_PROJECT_PROJECTION_ENABLED", "1")
    monkeypatch.setenv("MEMORYMASTER_PROJECT_PROJECTION_DIR", str(tmp_path / "projections"))
    monkeypatch.setenv("MEMORYMASTER_PROJECT_SESSION_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("MEMORYMASTER_PROJECT_PROJECTION_EPOCH", "fixture-epoch")
    monkeypatch.setenv("MEMORYMASTER_PROJECT_PROJECTION_PROJECTS", json.dumps([
        {"scope": f"project:p{i}", "context_key": f"p{i}", "project_root": str(tmp_path),
         "db_path": str(tmp_path / f"p{i}.db"), "tenant_id": None} for i in range(count)]))
    assert len(project_runtime.load_project_configs()) == (count if accepted else 0)

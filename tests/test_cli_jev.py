"""CLI wiring for Jev operations: status, revalidate, release-held, export, review queue, prune.

``jev-revalidate`` and ``jev-release-held`` lazily import modules built by other
tracks (``memorymaster.govern.jobs.revalidation`` and ``memorymaster.dreaming.held``);
the wiring is tested through ``sys.modules`` and a missing module is a clear error,
never a traceback.  Read commands never create the decisions ledger.
"""
from __future__ import annotations

import json
import runpy
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from memorymaster.decisions.ledger import DecisionLedger
from memorymaster.surfaces.cli import main

Q = runpy.run_path(str(Path(__file__).with_name("test_jev_review_queue.py")))
REVALIDATION = "memorymaster.govern.jobs.revalidation"
HELD = "memorymaster.dreaming.held"


@pytest.fixture()
def ledger_path(tmp_path, monkeypatch):
    for name in ("MEMORYMASTER_JEV_MODE", "MEMORYMASTER_JEV_DAILY_USD_CAP",
                 "MEMORYMASTER_DECISIONS_STATE_RETENTION_DAYS"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "decisions.db"
    monkeypatch.setenv("MEMORYMASTER_DECISIONS_DB", str(path))
    return path


def _run(capsys, argv):
    capsys.readouterr()
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _seed(path):
    ledger = DecisionLedger(path)
    Q["seed"](ledger, datetime.now(timezone.utc))
    return ledger


def test_jev_status_prints_metrics_json(tmp_path, ledger_path, capsys, monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_JEV_RECALL", "live")
    _seed(ledger_path)
    code, out, _ = _run(capsys, ["--db", str(tmp_path / "m.db"), "jev-status", "--days", "7"])
    assert code == 0
    payload = json.loads(out)
    assert payload["surfaces"]["recall"]["volume"] >= 40
    assert payload["configured_modes"]["recall"] == "live"
    assert not (tmp_path / "m.db").exists()  # no MemoryService for a ledger read


def test_jev_status_on_absent_ledger_is_empty_and_creates_nothing(tmp_path, ledger_path, capsys):
    code, out, _ = _run(capsys, ["--db", str(tmp_path / "m.db"), "jev-status"])
    assert code == 0 and json.loads(out)["surfaces"] == {}
    assert not ledger_path.exists()


def test_jev_review_queue_prints_the_weekly_queue(tmp_path, ledger_path, capsys):
    _seed(ledger_path)
    code, out, _ = _run(capsys, ["--db", str(tmp_path / "m.db"), "jev-review-queue", "--size", "5"])
    assert code == 0
    items = json.loads(out)["items"]
    assert 0 < len(items) <= 5 and all(item["reasons"] for item in items)


def test_jev_export_writes_jsonl_with_time_split(tmp_path, ledger_path, capsys):
    _seed(ledger_path)
    target = tmp_path / "out" / "decisions.jsonl"
    split = (datetime.now(timezone.utc) - timedelta(days=2) + timedelta(minutes=30)).isoformat()
    code, out, _ = _run(capsys, ["--db", str(tmp_path / "m.db"), "jev-export", "--output", str(target),
                                 "--split-at", split])
    assert code == 0
    rows = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
    assert json.loads(out)["rows"] == len(rows) > 40
    assert {row["split"] for row in rows} == {"train", "test"}
    assert "state_redacted" not in rows[0]


def test_jev_export_rejects_a_bad_timestamp(tmp_path, ledger_path, capsys):
    _seed(ledger_path)
    code, out, _ = _run(capsys, ["--db", str(tmp_path / "m.db"), "jev-export", "--output",
                                 str(tmp_path / "x.jsonl"), "--since", "yesterday"])
    assert code == 2 and "since" in out


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal"])
def test_jev_export_refuses_to_write_over_the_ledger(tmp_path, ledger_path, capsys, suffix):
    """``export_jsonl`` opens ``--output`` with mode 'w': pointed at the ledger it would truncate it to 0 bytes."""
    _seed(ledger_path)
    before = ledger_path.read_bytes()
    target = Path(str(ledger_path) + suffix)
    existed = target.exists()
    code, out, _ = _run(capsys, ["--db", str(tmp_path / "m.db"), "jev-export", "--output", str(target)])
    assert code == 2 and "ledger" in out, out
    assert ledger_path.read_bytes() == before
    assert target.exists() == existed


def test_jev_prune_nulls_old_state_only(tmp_path, ledger_path, capsys):
    ledger = DecisionLedger(ledger_path)
    now = datetime.now(timezone.utc)
    state = {"state": {"request": "x"}, "subjects": {}}
    Q["decision"](ledger, "old", ts=now - timedelta(days=3), state=state)
    Q["decision"](ledger, "new", ts=now - timedelta(hours=1), state=state)
    code, out, _ = _run(capsys, ["--db", str(tmp_path / "m.db"), "jev-prune"])
    assert code == 0 and json.loads(out)["pruned"] == 0  # default retention: 180 days
    code, out, _ = _run(capsys, ["--db", str(tmp_path / "m.db"), "jev-prune", "--retention-days", "2"])
    assert code == 0 and json.loads(out)["pruned"] == 1
    rows = {r["decision_id"]: r["state_redacted"] for r in ledger.query("SELECT * FROM decisions")}
    assert rows["old"] is None and rows["new"] is not None


def test_jev_revalidate_forwards_limits_to_the_revalidation_job(tmp_path, ledger_path, capsys, monkeypatch):
    calls = []

    def run(service, *, limit, max_usd, backfill):
        calls.append({"service": service, "limit": limit, "max_usd": max_usd, "backfill": backfill})
        return {"examined": 3, "revalidated": 1}

    monkeypatch.setitem(sys.modules, REVALIDATION, types.SimpleNamespace(run=run))
    monkeypatch.setenv("MEMORYMASTER_JEV_DAILY_USD_CAP", "1.25")
    db = tmp_path / "m.db"
    code, out, _ = _run(capsys, ["--db", str(db), "jev-revalidate", "--limit", "7", "--backfill"])
    assert code == 0 and json.loads(out) == {"examined": 3, "revalidated": 1}
    assert calls[0]["limit"] == 7 and calls[0]["backfill"] is True
    assert calls[0]["max_usd"] == 1.25  # bounded by the daily cap unless given
    assert calls[0]["service"].__class__.__name__ == "MemoryService"
    code, _, _ = _run(capsys, ["--db", str(db), "jev-revalidate", "--max-usd", "0.5"])
    assert code == 0 and calls[1] == {**calls[1], "limit": 50, "max_usd": 0.5, "backfill": False}


def test_jev_release_held_releases_as_operator(tmp_path, ledger_path, capsys, monkeypatch):
    calls = []

    def release(ledger_or_path, *, capture_id, candidate_id, actor):
        calls.append((ledger_or_path, capture_id, candidate_id, actor))
        return {"released": True}

    monkeypatch.setitem(sys.modules, HELD, types.SimpleNamespace(release=release))
    monkeypatch.setenv("MEMORYMASTER_CAPTURE_STATE_DB", str(tmp_path / "capture.db"))
    code, out, _ = _run(capsys, ["--db", str(tmp_path / "m.db"), "jev-release-held", "--capture-id", "42",
                                 "--candidate-id", "cand-abc"])
    assert code == 0 and json.loads(out) == {"released": True}
    ledger_arg, capture_id, candidate_id, actor = calls[0]
    assert (capture_id, candidate_id, actor) == (42, "cand-abc", "operator")
    assert Path(ledger_arg) == tmp_path / "capture.db"  # the Dreaming ledger, passed by path
    assert not (tmp_path / "m.db").exists()


@pytest.mark.parametrize("module,argv", [
    (REVALIDATION, ["jev-revalidate", "--limit", "1"]),
    (HELD, ["jev-release-held", "--capture-id", "1", "--candidate-id", "c"]),
])
def test_missing_module_is_a_clear_error(tmp_path, ledger_path, capsys, monkeypatch, module, argv):
    monkeypatch.setitem(sys.modules, module, None)  # import raises ModuleNotFoundError
    code, out, err = _run(capsys, ["--db", str(tmp_path / "m.db"), *argv])
    assert code == 2
    assert module in out + err and "not available" in out + err
    assert "Traceback" not in out + err


def test_import_error_inside_the_module_is_not_masked(tmp_path, ledger_path, capsys, monkeypatch):
    def broken(*_args, **_kwargs):
        raise ModuleNotFoundError("No module named 'some_dependency'", name="some_dependency")

    monkeypatch.setitem(sys.modules, REVALIDATION, types.SimpleNamespace(run=broken))
    code, out, err = _run(capsys, ["--db", str(tmp_path / "m.db"), "jev-revalidate"])
    assert code == 2 and "some_dependency" in out + err and "not available" not in out + err


def test_review_queue_survives_a_cp1252_pipe(tmp_path, ledger_path):
    """Agents pipe the CLI; on Windows that stdout is cp1252 while claim text carries
    arrows, check marks and accents. The queue must print, not exit 2 with nothing."""
    import os
    import subprocess

    ledger = DecisionLedger(ledger_path)
    Q["decision"](ledger, "arrow", ts=datetime.now(timezone.utc) - timedelta(days=1), surface="revalidate",
                  legacy='"keep_stale"', jev='"keep_stale"',
                  thresholds={"lifecycle.still_valid@v1": {"accept": 0.85, "low": 0.2}},
                  state={"state": {"claim": {"text": "deploy → staging ✅ configuración"}},
                         "subjects": {}},
                  items=[Q["item"]("arrow", "claim:10", "lifecycle.still_valid", 0.84)])
    env = {**os.environ, "PYTHONIOENCODING": "cp1252", "MEMORYMASTER_DECISIONS_DB": str(ledger_path)}
    done = subprocess.run(
        [sys.executable, "-m", "memorymaster.surfaces.cli", "--db", str(tmp_path / "m.db"), "jev-review-queue"],
        capture_output=True, env=env, cwd=str(Path(__file__).resolve().parents[1]), timeout=120,
    )
    assert done.returncode == 0, done.stderr.decode("cp1252", "replace")[-400:]
    assert r"deploy \u2192 staging" in done.stdout.decode("cp1252")  # a JSON escape, not a crash

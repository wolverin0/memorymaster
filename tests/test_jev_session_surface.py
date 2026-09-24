"""S8 SESSION: the SessionStart hook injects confirmed claims only, ranked by Jev when live.

The hook template is loaded in-process against a disposable database and a temp
decisions ledger; the transport is scripted, so nothing leaves the machine.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

from memorymaster.core.lifecycle import transition_claim
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.decisions import engine as decisions_engine
from memorymaster.decisions.config import DecisionConfig
from memorymaster.decisions.credentials import ApiKey
from memorymaster.decisions.engine import DecisionEngine
from memorymaster.decisions.ledger import DecisionLedger
from memorymaster.decisions.transport import ParsedAnswer, TransportResult
from memorymaster.recall import jev_surfaces as jev

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "memorymaster" / "config_templates" / "hooks" / "memorymaster-session-start.py"
KEY = ApiKey("ts-test-" + "S" * 24)  # synthetic
SESSION = "session-start-fixture"


class ScriptedTransport:
    def __init__(self, scores=None, *, outcome="ok"):
        self.scores = scores or {}
        self.outcome = outcome
        self.calls: list[dict] = []

    def send(self, payload, *, expected, timeout_s, max_retries=0):
        self.calls.append({"payload": payload, "timeout_s": timeout_s, "max_retries": max_retries})
        if self.outcome != "ok":
            return TransportResult(None, None, 10, 1, self.outcome)
        answers = {}
        for wire_id, schema in expected.items():
            value = self.scores.get(int(wire_id.split("::claim:", 1)[1]), 0.1)
            levels = [str(level) for level in schema.levels]
            probabilities = {level: 0.0 for level in levels}
            probabilities[levels[-1]] = value
            probabilities[levels[0]] = round(1.0 - value, 6)
            answers[wire_id] = ParsedAnswer("score", value, probabilities, None, levels[-1])
        return TransportResult(200, {"model": "jev-1.13.0"}, 30, 1, "ok", model_served="jev-1.13.0",
                               tokens_in=200, tokens_out=0, cost_usd=200 * 0.042e-6, answers=answers)

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith(("MEMORYMASTER_JEV", "MEMORYMASTER_DECISIONS", "TYPESAFE_")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MEMORYMASTER_DECISIONS_DB", str(tmp_path / "decisions.db"))
    monkeypatch.setenv("MEMORYMASTER_RECALL_STATE_DIR", str(tmp_path / "delivery"))


def _hook(tmp_path, monkeypatch, db):
    spec = importlib.util.spec_from_file_location("jev_session_start_hook", HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "DB_PATH", db)
    monkeypatch.setattr(module, "PROFILE_PATH", tmp_path / "no-profile.md")
    monkeypatch.setattr(module, "WIKI_ROOT", tmp_path / "no-wiki")
    monkeypatch.setattr(module, "_log", lambda *_a, **_k: None)
    return module


def _run(module, monkeypatch, capsys, cwd: Path) -> str:
    monkeypatch.chdir(cwd)
    payload = {"session_id": SESSION, "cwd": str(cwd), "hook_event_name": "SessionStart"}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    with pytest.raises(SystemExit):
        module.main()
    out = capsys.readouterr().out
    return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out else ""


def _fixture(tmp_path, statuses):
    """Claims in ``project:jevproj`` created oldest-first; returns ids in creation order."""
    project = tmp_path / "jevproj"
    project.mkdir()
    db = tmp_path / "session.db"
    service = MemoryService(db, workspace_root=tmp_path)
    service.init_db()
    ids = []
    for index, status in enumerate(statuses):
        visibility = "private" if status == "private" else "public"
        claim = service.ingest(f"session fixture claim number {index} about the jevproj build",
                               [CitationInput(source="test://session", locator=f"fixture-{index}")],
                               scope="project:jevproj", visibility=visibility)
        if status in {"confirmed", "stale", "private"}:
            transition_claim(service.store, claim.id, "confirmed", "test fixture")
        if status == "stale":
            transition_claim(service.store, claim.id, "stale", "test fixture")
        ids.append(claim.id)
    with sqlite3.connect(db) as conn:  # disposable fixture: distinct creation times, oldest first
        for index, claim_id in enumerate(ids):
            conn.execute("UPDATE claims SET created_at = ? WHERE id = ?",
                         (f"2026-09-{1 + index // 24:02d}T{index % 24:02d}:00:00+00:00", claim_id))
    return db, project, ids


def _injected_ids(context: str) -> list[int]:
    ids = []
    for line in context.splitlines():
        stripped = line.strip()
        if stripped.startswith("- #"):
            ids.append(int(stripped[3:].split(" ", 1)[0]))
    return ids


def _install(monkeypatch, tmp_path, transport, mode="live"):
    monkeypatch.setenv("MEMORYMASTER_JEV_MODE", mode)
    engine = DecisionEngine(DecisionConfig.from_env(), transport_factory=lambda _k: transport, key_lookup=lambda: KEY)
    monkeypatch.setattr(decisions_engine, "default_engine", lambda: engine)


def test_session_start_injects_only_confirmed_claims(tmp_path, monkeypatch, capsys):
    db, project, ids = _fixture(tmp_path, ["confirmed", "candidate", "stale", "confirmed", "candidate"])
    module = _hook(tmp_path, monkeypatch, db)
    monkeypatch.setattr(decisions_engine, "default_engine", lambda: pytest.fail("engine used while off"))

    context = _run(module, monkeypatch, capsys, project)

    assert _injected_ids(context) == [ids[3], ids[0]]
    assert "/candidate]" not in context and "/stale]" not in context
    assert not (tmp_path / "decisions.db").exists()


def test_off_mode_injects_the_five_most_recent_confirmed(tmp_path, monkeypatch, capsys):
    db, project, ids = _fixture(tmp_path, ["confirmed"] * 8)
    module = _hook(tmp_path, monkeypatch, db)
    context = _run(module, monkeypatch, capsys, project)
    assert _injected_ids(context) == list(reversed(ids))[:5]


def test_live_ranks_the_recent_confirmed_pool_by_relevance(tmp_path, monkeypatch, capsys):
    db, project, ids = _fixture(tmp_path, ["confirmed"] * 8 + ["private", "candidate"])
    scores = {cid: 0.1 for cid in ids}
    wanted = [ids[0], ids[2], ids[1], ids[5], ids[7]]
    for rank, cid in enumerate(wanted):
        scores[cid] = 0.95 - 0.05 * rank
    transport = ScriptedTransport(scores)
    _install(monkeypatch, tmp_path, transport)
    module = _hook(tmp_path, monkeypatch, db)

    context = _run(module, monkeypatch, capsys, project)

    # B1: the newest confirmed claim is private: never sent, but the caller is
    # authorized to see it, so it keeps its legacy slot (first) and Jev fills the rest.
    assert _injected_ids(context) == [ids[8], *wanted[:4]]
    payload = transport.calls[0]["payload"]
    asked = {int(wire.split("::claim:", 1)[1]) for wire in payload["questions"]}
    assert asked == set(ids[:8])  # confirmed + public only; private and candidate are never sent
    assert payload["state"] == {"project": "jevproj"}
    assert transport.calls[0]["max_retries"] == 0
    ledger = DecisionLedger(tmp_path / "decisions.db")
    decision = ledger.query("SELECT * FROM decisions")[0]
    assert decision["surface"] == "session" and decision["mode"] == "live" and decision["fallback_reason"] is None
    assert decision["session_key"] == jev.decision_session_key(SESSION)
    assert json.loads(decision["baseline_features_json"])["passthrough"] == 1
    exposed = ledger.query("SELECT DISTINCT item_ref FROM decision_items WHERE exposed = 1 AND delivered = 1")
    assert {row["item_ref"] for row in exposed} == {f"claim:{cid}" for cid in wanted[:4]}


def test_live_keeps_every_private_legacy_claim_in_its_slot(tmp_path, monkeypatch, capsys):
    db, project, ids = _fixture(tmp_path, ["confirmed", "private", "confirmed", "private", "confirmed",
                                           "confirmed"])
    scores = {ids[0]: 0.9, ids[2]: 0.8}
    transport = ScriptedTransport(scores)
    _install(monkeypatch, tmp_path, transport)
    module = _hook(tmp_path, monkeypatch, db)

    context = _run(module, monkeypatch, capsys, project)

    # legacy (newest first): 5c, 4c, 3p, 2c, 1p -> private slots 2 and 4 stay, Jev's top 3
    # (0, 2, then 5 by recency among the ties) fill slots 0, 1 and 3
    assert _injected_ids(context) == [ids[0], ids[2], ids[3], ids[5], ids[1]]
    asked = {int(wire.split("::claim:", 1)[1]) for wire in transport.calls[0]["payload"]["questions"]}
    assert asked == {ids[0], ids[2], ids[4], ids[5]}


def test_failure_falls_back_to_the_five_most_recent_confirmed(tmp_path, monkeypatch, capsys):
    db, project, ids = _fixture(tmp_path, ["confirmed"] * 7)
    _install(monkeypatch, tmp_path, ScriptedTransport(outcome="http_5xx"))
    module = _hook(tmp_path, monkeypatch, db)

    context = _run(module, monkeypatch, capsys, project)

    assert _injected_ids(context) == list(reversed(ids))[:5]
    decision = DecisionLedger(tmp_path / "decisions.db").query("SELECT * FROM decisions")[0]
    assert decision["fallback_reason"] == "http_5xx"


def test_pool_is_limited_to_thirty_most_recent(tmp_path, monkeypatch, capsys):
    db, project, ids = _fixture(tmp_path, ["confirmed"] * 33)
    transport = ScriptedTransport({})
    _install(monkeypatch, tmp_path, transport, mode="shadow")
    module = _hook(tmp_path, monkeypatch, db)

    context = _run(module, monkeypatch, capsys, project)

    asked = {int(wire.split("::claim:", 1)[1]) for wire in transport.calls[0]["payload"]["questions"]}
    assert asked == set(ids[-30:])
    assert _injected_ids(context) == list(reversed(ids))[:5]  # shadow keeps legacy


def test_a_sensitive_public_claim_is_never_sent_and_keeps_its_slot(tmp_path, monkeypatch, capsys):
    db, project, ids = _fixture(tmp_path, ["confirmed"] * 7)
    with sqlite3.connect(db) as conn:  # disposable fixture: a stored redaction marker makes it sensitive
        conn.execute("UPDATE claims SET text = ? WHERE id = ?",
                     ("session fixture SECRETMARKER [REDACTED:api_key] for jevproj", ids[5]))
    transport = ScriptedTransport({ids[0]: 0.9, ids[1]: 0.8})
    _install(monkeypatch, tmp_path, transport)
    module = _hook(tmp_path, monkeypatch, db)

    context = _run(module, monkeypatch, capsys, project)

    assert "SECRETMARKER" not in json.dumps(transport.calls[0]["payload"])
    asked = {int(wire.split("::claim:", 1)[1]) for wire in transport.calls[0]["payload"]["questions"]}
    assert ids[5] not in asked and asked == set(ids) - {ids[5]}
    # legacy newest first: 6, 5 (sensitive), 4, 3, 2 -> slot 1 stays, Jev's top 4 fill the rest
    assert _injected_ids(context) == [ids[0], ids[5], ids[1], ids[6], ids[4]]

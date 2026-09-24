"""F-21: the bulk settled-proposal closer resolves as automation, never jev.

Verifier note on F-21: ``scripts/close_settled_proposals.py`` is a deterministic
accounting resolver (``apply_on_approve=False``), yet it recorded every
resolution as the operator (``source: human_override``), and it would approve
``source: jev`` proposals, which the contract reserves for the operator.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from memorymaster.core.service import MemoryService
from memorymaster.govern import steward

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "close_settled_proposals.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("close_settled_proposals", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_settled_proposals_are_closed_as_automation_and_jev_ones_are_left(tmp_path, monkeypatch):
    db = tmp_path / "settled.db"
    MemoryService(db, workspace_root=tmp_path).init_db()
    # Both claims are gone, so both proposals classify as settled accounting.
    proposals = [
        {"proposal_event_id": 11, "claim_id": 9001, "payload": {"decision": "stale"}},
        {"proposal_event_id": 12, "claim_id": 9002,
         "payload": {"source": "jev", "decision": "stale"}},
    ]
    resolved: list[dict] = []
    monkeypatch.setattr(steward, "list_steward_proposals", lambda *a, **k: list(proposals))
    monkeypatch.setattr(steward, "resolve_steward_proposal",
                        lambda svc, **kwargs: resolved.append(kwargs) or {"ok": True})

    assert _load_script().main(["--db", str(db), "--workspace", str(tmp_path), "--apply"]) == 0

    assert resolved == [{
        "action": "approve",
        "proposal_event_id": 11,
        "apply_on_approve": False,
        "actor": "automation",
    }]

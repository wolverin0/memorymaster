"""Cross-track contract (review B1): S1 rows written by the real engine gate archival.

``engine.decide`` stores the surface lowercased (``revalidate``) and
JSON-encodes ``action_taken`` (``'"no_longer_useful"'``). The archive gate
must read that exact shape, or ``scheduled_archive`` can never archive
anything after the 4.9.0 merge. The module skips until the decisions engine
(track C) is on the import path.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("memorymaster.decisions.engine", reason="needs the 4.9.0 decisions engine")

from memorymaster.core import lifecycle  # noqa: E402
from memorymaster.core.models import CitationInput  # noqa: E402
from memorymaster.core.service import MemoryService  # noqa: E402
from memorymaster.decisions import archive_gate  # noqa: E402
from memorymaster.decisions import questions as q  # noqa: E402
from memorymaster.decisions.config import DecisionConfig  # noqa: E402
from memorymaster.decisions.credentials import ApiKey  # noqa: E402
from memorymaster.decisions.engine import DecisionContext, DecisionEngine, DecisionItem, JevChoice  # noqa: E402
from memorymaster.decisions.transport import ParsedAnswer, TransportResult  # noqa: E402
from memorymaster.govern.jobs import scheduled_archive  # noqa: E402
from memorymaster.recall import qdrant_outbox  # noqa: E402

KEY = ApiKey("ts-test-" + "F" * 24)  # synthetic
S1_QUESTIONS = ("lifecycle.still_valid", "lifecycle.durable", "lifecycle.useful_future")


class LowScoreTransport:
    """Offline transport: every S1 noul question answers 0.1 (no real network)."""

    def send(self, payload, *, expected, timeout_s, max_retries=0):
        answers = {wire_id: ParsedAnswer("noul", 0.1, {"noul": 0.1}) for wire_id in expected}
        return TransportResult(200, {"model": "jev-test"}, 5, 1, "ok", model_served="jev-test",
                               tokens_in=100, tokens_out=3, cost_usd=0.0, answers=answers)

    def close(self):
        pass


def s1_choose(ref: str):
    def choose(answers) -> JevChoice:
        low = all((answers.noul(question, ref) or 0.0) < 0.3 for question in S1_QUESTIONS)
        return JevChoice(action="no_longer_useful" if low else "keep_stale", safe_alternative="keep_stale")
    return choose


def decide_s1(ledger: Path, claim, *, mode: str = "live"):
    config = DecisionConfig.from_env({
        "MEMORYMASTER_JEV_MODE": mode,
        "MEMORYMASTER_JEV_EXPLORE_REVALIDATE": "0",
        "MEMORYMASTER_DECISIONS_DB": str(ledger),
    })
    engine = DecisionEngine(config, transport_factory=lambda _key: LowScoreTransport(), key_lookup=lambda: KEY)
    ref = f"claim:{claim.id}"
    state, bound = q.build_revalidate(
        {"text": claim.text, "claim_type": claim.claim_type, "scope": claim.scope},
        age_bucket="unknown", item_ref=ref,
    )
    # Mixed case on purpose: the engine normalizes the surface before writing.
    return engine.decide("REVALIDATE", state=state, questions=bound, items=[DecisionItem(ref)],
                         legacy_action="keep_stale", choose=s1_choose(ref),
                         context=DecisionContext(kind="batch"))


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(qdrant_outbox.ENV_OUTBOX_DIR, str(tmp_path / "qdrant-outbox"))
    monkeypatch.delenv("QDRANT_URL", raising=False)
    ledger = tmp_path / "decisions.db"
    monkeypatch.setenv("MEMORYMASTER_DECISIONS_DB", str(ledger))
    service = MemoryService(tmp_path / "archive.db", workspace_root=tmp_path)
    service.init_db()
    return service, ledger


def _stale_unused(service, text: str):
    claim = service.ingest(text, [CitationInput(source="test://s1-engine")], confidence=0.4)
    lifecycle.transition_claim(service.store, claim.id, "confirmed", reason="fixture", event_type="validator")
    lifecycle.transition_claim(service.store, claim.id, "stale", reason="fixture", event_type="decay")
    old = (datetime.now(timezone.utc) - timedelta(days=30)).replace(microsecond=0).isoformat()
    with service.store.connect() as conn:
        conn.execute("UPDATE claims SET created_at=?, updated_at=?, access_count=0 WHERE id=?",
                     (old, old, claim.id))
        conn.commit()
    return service.store.get_claim(claim.id, include_citations=False)


def test_live_engine_judgment_is_seen_and_archives(env):
    service, ledger = env
    claim = _stale_unused(service, "stale claim the real S1 engine judged no longer useful")

    decision = decide_s1(ledger, claim)

    assert decision.mode == "live" and decision.fallback_reason is None, decision
    assert decision.action == "no_longer_useful"
    with sqlite3.connect(ledger) as conn:
        stored = conn.execute("SELECT surface, mode, action_taken FROM decisions").fetchall()
    assert stored == [("revalidate", "live", '"no_longer_useful"')], "engine row shape changed"

    assert archive_gate.has_no_longer_useful_judgment(claim.id, not_before=claim.updated_at) is True
    result = scheduled_archive.run(service, older_than_days=14)
    assert result["archived"] == 1
    assert service.store.get_claim(claim.id, include_citations=False).status == "archived"


def test_shadow_engine_decision_never_archives(env):
    service, ledger = env
    claim = _stale_unused(service, "stale claim judged by S1 in shadow mode only")

    decision = decide_s1(ledger, claim, mode="shadow")

    assert decision.mode == "shadow"
    assert archive_gate.has_no_longer_useful_judgment(claim.id) is False
    assert scheduled_archive.run(service, older_than_days=14)["archived"] == 0
    assert service.store.get_claim(claim.id, include_citations=False).status == "stale"

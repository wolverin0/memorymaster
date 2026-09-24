"""S2 RECALL wired onto the Jev decision engine (4.9.0 wave 2): prompt hook and MCP paths.

Every test runs against disposable SQLite files, a temp decisions ledger and a
scripted in-process transport: no provider is ever called and nothing under the
user's home is written.
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

import pytest

from memorymaster.core import hook_log
from memorymaster.core.lifecycle import transition_claim
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.decisions import engine as decisions_engine
from memorymaster.decisions.config import DecisionConfig
from memorymaster.decisions.credentials import ApiKey
from memorymaster.decisions.engine import DecisionEngine
from memorymaster.decisions.ledger import DecisionLedger
from memorymaster.decisions.transport import ParsedAnswer, TransportResult
from memorymaster.recall import context_hook, delivery
from memorymaster.recall import jev_surfaces as jev

KEY = ApiKey("ts-test-" + "R" * 24)  # synthetic
QUERY = "explain zebracache behaviour"
SESSION = "Session-ABC-123"


# ------------------------------------------------------------------ fakes ---

def _parsed(schema, value):
    if schema.primitive == "noul":
        return ParsedAnswer("noul", value, {"noul": value})
    if schema.primitive == "score":
        levels = [str(level) for level in schema.levels]
        probabilities = {level: 0.0 for level in levels}
        probabilities[levels[-1]] = value
        probabilities[levels[0]] = round(1.0 - value, 6)
        return ParsedAnswer("score", value, probabilities, None, levels[-1] if value >= 0.5 else levels[0])
    raise AssertionError("choice answers are scripted per test")


class ScriptedTransport:
    """In-process stand-in for the TypeSafe transport (never touches the network)."""

    def __init__(self, answer=None, *, delay: float = 0.0, outcome: str = "ok"):
        self.answer = answer
        self.delay = delay
        self.outcome = outcome
        self.calls: list[dict] = []

    def send(self, payload, *, expected, timeout_s, max_retries=0):
        self.calls.append({"payload": payload, "timeout_s": timeout_s, "max_retries": max_retries,
                           "expected": dict(expected)})
        if self.delay:
            time.sleep(self.delay)
        if self.outcome != "ok":
            return TransportResult(None, None, 12, 1, self.outcome)
        answers = {wire_id: self.answer(wire_id, schema) for wire_id, schema in expected.items()}
        return TransportResult(200, {"model": "jev-1.13.0"}, 40, 1, "ok", model_served="jev-1.13.0",
                               tokens_in=300, tokens_out=0, cost_usd=300 * 0.042e-6, answers=answers)

    def close(self):
        pass


def recall_answers(plan):
    """``plan[claim_id] = dict(relevant=, usable=, conflict=, instruction=)``; unknown ids are weak."""
    names = {"recall.relevant": "relevant", "recall.usable_evidence": "usable",
             "recall.contradicts_request": "conflict", "recall.instruction_like": "instruction"}

    def answer(wire_id, schema):
        question_id, ref = wire_id.split("::", 1)
        values = plan.get(int(ref.split(":", 1)[1]), {})
        return _parsed(schema, values.get(names[question_id], 0.1))

    return answer


def install_engine(monkeypatch, tmp_path, transport, **env):
    # A hook deadline no loaded runner reaches unless the test is about the deadline (Ubuntu CI
    # 2026-09-24 fell back to legacy at the 900 ms default); deadline tests pass their own.
    settings = {"MEMORYMASTER_JEV_MODE": "live", "MEMORYMASTER_JEV_EXPLORE_RECALL": "0",
                "MEMORYMASTER_JEV_HOOK_DEADLINE_MS": "10000"}
    settings.update(env)
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    engine = DecisionEngine(DecisionConfig.from_env(), transport_factory=lambda _key: transport,
                            key_lookup=lambda: KEY)
    monkeypatch.setattr(decisions_engine, "default_engine", lambda: engine)
    return engine


def ledger_rows(tmp_path, sql, params=()):
    return DecisionLedger(tmp_path / "decisions.db").query(sql, params)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Temp ledger/spool/delivery/log locations; every MEMORYMASTER_JEV_* starts unset."""
    import os

    for name in list(os.environ):
        if name.startswith(("MEMORYMASTER_JEV", "MEMORYMASTER_DECISIONS", "TYPESAFE_")):
            monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("MEMORYMASTER_DECISIONS_DB", str(tmp_path / "decisions.db"))
    monkeypatch.setenv("MEMORYMASTER_SPOOL_DIR", str(tmp_path / "spool"))
    monkeypatch.setenv("MEMORYMASTER_RECALL_STATE_DIR", str(tmp_path / "delivery"))
    monkeypatch.setenv("MEMORYMASTER_SCOPE_DEFAULT", "project:allowed")
    monkeypatch.delenv("QDRANT_URL", raising=False)
    monkeypatch.setattr(hook_log, "_STATE_DIR", tmp_path / "hook_state")
    monkeypatch.setattr(hook_log, "_LOG_FILE", tmp_path / "hook_state" / "hook.log")


def build_db(tmp_path, texts, *, scope="project:allowed", name="recall.db"):
    db = tmp_path / name
    service = MemoryService(db, workspace_root=tmp_path)
    service.init_db()
    ids = []
    for index, text in enumerate(texts):
        # distinct sources: the per-source diversity cap must not hide fixtures
        claim = service.ingest(text, [CitationInput(source=f"test://jev-{index}", locator="fixture")],
                               scope=scope, source_agent=f"fixture-agent-{index}")
        transition_claim(service.store, claim.id, "confirmed", "test fixture")
        ids.append(claim.id)
    return db, ids


TEXTS = [
    "zebracache warms the prompt index before the first recall of a session",
    "zebracache stores hashed prompt fingerprints for five minutes only",
    "zebracache must never persist memory content outside the ledger file",
    "zebracache eviction runs when the spool directory exceeds its quota",
    "zebracache metrics are exported through the operational review report",
    "zebracache was introduced to stop duplicate recall injections in panes",
]


def hook_data(tmp_path):
    return {"session_id": SESSION, "cwd": str(tmp_path / "workspace" / "memorymaster"), "prompt": QUERY}


def legacy_recall(db):
    return context_hook.recall(QUERY, db_path=str(db), skip_qdrant=True, return_ids=True)


# ------------------------------------------------------- S2 prompt hook ---

def test_off_mode_output_is_byte_identical_and_sends_nothing(tmp_path, monkeypatch):
    db, fixture_ids = build_db(tmp_path, TEXTS)
    monkeypatch.setattr(decisions_engine, "default_engine", lambda: pytest.fail("engine used while off"))
    plain, plain_ids = legacy_recall(db)
    hooked, hooked_ids = context_hook.recall(QUERY, db_path=str(db), skip_qdrant=True, return_ids=True,
                                             hook_data=hook_data(tmp_path))
    assert plain_ids and hooked == plain and hooked_ids == plain_ids
    by_id = dict(zip(fixture_ids, TEXTS))
    assert hooked == "\n".join(["# Memory Context", ""] + [f"- {by_id[i]}" for i in plain_ids])
    assert not (tmp_path / "decisions.db").exists()


def test_live_orders_by_relevance_adapts_k_and_labels_flags(tmp_path, monkeypatch):
    db, _ = build_db(tmp_path, TEXTS)
    wide = "zebracache"  # one content token: the lexical stream returns every fixture
    _, legacy_ids = context_hook.recall(wide, db_path=str(db), skip_qdrant=True, return_ids=True)
    assert len(legacy_ids) >= 5
    best, second, third = legacy_ids[-1], legacy_ids[-2], legacy_ids[-3]
    plan = {cid: {"relevant": 0.2, "usable": 0.2} for cid in legacy_ids}
    plan[best] = {"relevant": 0.95, "usable": 0.9, "conflict": 0.9}
    plan[second] = {"relevant": 0.85, "usable": 0.8, "instruction": 0.8}
    plan[third] = {"relevant": 0.75, "usable": 0.7}
    transport = ScriptedTransport(recall_answers(plan))
    install_engine(monkeypatch, tmp_path, transport)

    out, ids = context_hook.recall(wide, db_path=str(db), skip_qdrant=True, return_ids=True,
                                   hook_data=hook_data(tmp_path))

    assert ids == [best, second, third]
    lines = out.splitlines()
    assert lines[:2] == ["# Memory Context", ""] and len(lines) == 5
    assert lines[2].startswith("- " + jev.LABEL_CONFLICT + " ")
    assert lines[3].startswith("- " + jev.LABEL_INSTRUCTION + " ")
    assert "[flag:" not in lines[4]
    # One request, hook deadline, zero retries; state = request + project label only.
    assert len(transport.calls) == 1
    call = transport.calls[0]
    # hook kind: no retries (batch retries 3 times), and at most the time left of the hook deadline
    assert 0 < call["timeout_s"] <= 10.0 and call["max_retries"] == 0
    assert call["payload"]["state"] == {"request": wide, "project": "memorymaster"}
    asked = {wire.split("::", 1)[1] for wire in call["payload"]["questions"]}
    assert asked == {f"claim:{cid}" for cid in legacy_ids}
    assert len(call["payload"]["questions"]) == 4 * len(legacy_ids)

    decision = ledger_rows(tmp_path, "SELECT * FROM decisions")[0]
    assert decision["surface"] == "recall" and decision["mode"] == "live"
    assert decision["fallback_reason"] is None and decision["exploration_arm"] == "policy"
    assert decision["session_key"] == jev.decision_session_key(SESSION)
    assert json.loads(decision["action_taken"]) == [f"claim:{cid}" for cid in (best, second, third)]
    assert json.loads(decision["legacy_action"]) == [f"claim:{cid}" for cid in legacy_ids]
    items = ledger_rows(tmp_path, "SELECT item_ref, MAX(exposed) AS exposed, MAX(delivered) AS delivered "
                                  "FROM decision_items GROUP BY item_ref")
    exposed = {row["item_ref"] for row in items if row["exposed"]}
    delivered = {row["item_ref"] for row in items if row["delivered"]}
    assert exposed == delivered == {f"claim:{cid}" for cid in (best, second, third)}


def test_delivery_suppression_is_logged_as_exposed_but_not_delivered(tmp_path, monkeypatch):
    db, _ = build_db(tmp_path, TEXTS)
    _, legacy_ids = legacy_recall(db)
    plan = {cid: {"relevant": 0.9 - 0.1 * n, "usable": 0.9} for n, cid in enumerate(legacy_ids)}
    install_engine(monkeypatch, tmp_path, ScriptedTransport(recall_answers(plan)))
    data = hook_data(tmp_path)

    first = context_hook.recall(QUERY, db_path=str(db), skip_qdrant=True, hook_data=data)
    assert delivery.deliver(data, delivery.recall_block(first), lambda _output: None)
    second = context_hook.recall(QUERY, db_path=str(db), skip_qdrant=True, hook_data=data)
    assert second == first
    assert not delivery.would_deliver(data, delivery.recall_block(second))

    decisions = ledger_rows(tmp_path, "SELECT decision_id FROM decisions ORDER BY ts")
    assert len(decisions) == 2
    for decision, expected_delivered in zip(decisions, (1, 0)):
        rows = ledger_rows(tmp_path, "SELECT MAX(exposed) AS e, MAX(delivered) AS d FROM decision_items "
                                     "WHERE decision_id = ? AND exposed = 1 GROUP BY item_ref",
                           (decision["decision_id"],))
        assert rows and all(row["e"] == 1 and row["d"] == expected_delivered for row in rows)


@pytest.mark.parametrize("outcome", ["http_5xx", "malformed", "http_429"])
def test_transport_failure_keeps_legacy_output_and_logs_fallback(tmp_path, monkeypatch, outcome):
    db, _ = build_db(tmp_path, TEXTS)
    legacy_out, legacy_ids = legacy_recall(db)
    install_engine(monkeypatch, tmp_path, ScriptedTransport(outcome=outcome))

    out, ids = context_hook.recall(QUERY, db_path=str(db), skip_qdrant=True, return_ids=True,
                                   hook_data=hook_data(tmp_path))

    assert (out, ids) == (legacy_out, legacy_ids)
    decision = ledger_rows(tmp_path, "SELECT * FROM decisions")[0]
    assert decision["fallback_reason"] == outcome and decision["transport_outcome"] == outcome
    exposed = ledger_rows(tmp_path, "SELECT DISTINCT item_ref FROM decision_items WHERE exposed = 1")
    assert {row["item_ref"] for row in exposed} == {f"claim:{cid}" for cid in legacy_ids}


def test_timeout_returns_legacy_within_the_deadline(tmp_path, monkeypatch):
    db, _ = build_db(tmp_path, TEXTS)
    legacy_out, _ = legacy_recall(db)
    plan = {}
    install_engine(monkeypatch, tmp_path, ScriptedTransport(recall_answers(plan), delay=3.0),
                   MEMORYMASTER_JEV_HOOK_DEADLINE_MS="200")

    started = time.perf_counter()
    out = context_hook.recall(QUERY, db_path=str(db), skip_qdrant=True, hook_data=hook_data(tmp_path))
    elapsed = time.perf_counter() - started

    assert out == legacy_out
    assert elapsed < 2.0
    decision = ledger_rows(tmp_path, "SELECT * FROM decisions")[0]
    assert decision["fallback_reason"] == "timeout" and decision["mode"] == "live"


def test_shadow_mode_keeps_legacy_and_logs_jev_action(tmp_path, monkeypatch):
    db, _ = build_db(tmp_path, TEXTS)
    legacy_out, legacy_ids = legacy_recall(db)
    plan = {cid: {"relevant": 0.1 + 0.1 * n, "usable": 0.9} for n, cid in enumerate(legacy_ids)}
    install_engine(monkeypatch, tmp_path, ScriptedTransport(recall_answers(plan)), MEMORYMASTER_JEV_MODE="shadow")

    out = context_hook.recall(QUERY, db_path=str(db), skip_qdrant=True, hook_data=hook_data(tmp_path))

    assert out == legacy_out
    decision = ledger_rows(tmp_path, "SELECT * FROM decisions")[0]
    assert decision["mode"] == "shadow" and decision["fallback_reason"] is None
    assert json.loads(decision["action_taken"]) == [f"claim:{cid}" for cid in legacy_ids]
    assert json.loads(decision["jev_action"]) == [f"claim:{cid}" for cid in reversed(legacy_ids)][:5]


def test_exploration_is_logged_with_engine_propensities(tmp_path, monkeypatch):
    db, _ = build_db(tmp_path, TEXTS)
    _, legacy_ids = legacy_recall(db)
    plan = {cid: {"relevant": 0.9 - 0.1 * n, "usable": 0.9} for n, cid in enumerate(legacy_ids)}
    install_engine(monkeypatch, tmp_path, ScriptedTransport(recall_answers(plan)),
                   MEMORYMASTER_JEV_EXPLORE_RECALL="1")

    _, ids = context_hook.recall(QUERY, db_path=str(db), skip_qdrant=True, return_ids=True,
                                 hook_data=hook_data(tmp_path))

    decision = ledger_rows(tmp_path, "SELECT * FROM decisions")[0]
    assert decision["exploration_arm"] == "explore_order"
    propensities = json.loads(decision["action_propensities_json"])
    assert math.isclose(sum(entry["p"] for entry in propensities), 1.0, abs_tol=1e-9)
    assert 0.0 < decision["chosen_propensity"] < 1.0
    assert ids == [int(ref.split(":")[1]) for ref in json.loads(decision["action_taken"])]


def test_small_budget_caps_k_so_every_exposed_item_is_rendered(tmp_path, monkeypatch):
    long_texts = [text + " " + ("padding words for the budget test " * 3) for text in TEXTS]
    db, fixture_ids = build_db(tmp_path, long_texts)
    budget = 110
    _, legacy_ids = context_hook.recall(QUERY, db_path=str(db), skip_qdrant=True, return_ids=True, budget=budget)
    plan = {cid: {"relevant": 0.9, "usable": 0.9, "conflict": 0.9, "instruction": 0.9} for cid in legacy_ids}
    for cid in fixture_ids:
        plan.setdefault(cid, {"relevant": 0.9, "usable": 0.9, "conflict": 0.9, "instruction": 0.9})
    install_engine(monkeypatch, tmp_path, ScriptedTransport(recall_answers(plan)))

    _, ids = context_hook.recall(QUERY, db_path=str(db), skip_qdrant=True, return_ids=True, budget=budget,
                                 hook_data=hook_data(tmp_path))

    exposed = ledger_rows(tmp_path, "SELECT DISTINCT item_ref FROM decision_items WHERE exposed = 1")
    assert ids and {f"claim:{cid}" for cid in ids} == {row["item_ref"] for row in exposed}


def test_decide_recall_asks_about_top_20_only(tmp_path, monkeypatch):
    transport = ScriptedTransport(recall_answers({}))
    install_engine(monkeypatch, tmp_path, transport)
    candidates = [jev.RecallCandidate(cid, f"memory number {cid}") for cid in range(1, 26)]

    selection = jev.decide_recall("which memory", candidates, legacy_ids=list(range(1, 26)),
                                  project_label="demo", k_cap=5)

    asked = {wire.split("::", 1)[1] for wire in transport.calls[0]["payload"]["questions"]}
    assert asked == {f"claim:{cid}" for cid in range(1, 21)}
    assert selection is not None and 2 <= len(selection.claim_ids) <= 5
    assert set(selection.claim_ids) <= set(range(1, 21))


def test_budget_cap_counts_the_largest_candidates():
    assert jev.budget_cap([10, 50, 30], 90) == 3
    assert jev.budget_cap([10, 50, 30], 89) == 2
    assert jev.budget_cap([10, 50, 30], 49) == 0
    assert jev.budget_cap([10, 50, 30], None) == 3


# ------------------------------------------------------------- delivery ---

def test_would_deliver_predicts_deliver_without_writing_state(tmp_path):
    state = tmp_path / "state"
    data = {"session_id": "s-1", "cwd": "project-a"}
    assert delivery.would_deliver(data, "memory", state_dir=state, now=10)
    assert not state.exists()
    assert delivery.deliver(data, "memory", lambda _o: None, state_dir=state, now=10)
    assert not delivery.would_deliver(data, "memory", state_dir=state, now=11)
    assert delivery.would_deliver(data, "other", state_dir=state, now=11)
    assert delivery.would_deliver({**data, "cwd": "project-b"}, "memory", state_dir=state, now=11)
    assert not delivery.would_deliver(data, "   ", state_dir=state, now=11)
    assert delivery.would_deliver({}, "memory", state_dir=state, now=11)
    assert delivery.recall_block("ctx") == "[MemoryMaster recall]\nctx"


def test_session_key_is_normalized_and_hashed():
    key = jev.decision_session_key(SESSION)
    assert key and key.startswith("session:") and SESSION.lower() not in key
    assert jev.decision_session_key("  session-abc-123 ") == key
    assert jev.decision_session_key(SESSION, "tenant-a") != key
    assert jev.decision_session_key("") is None and jev.decision_session_key(None) is None


# ------------------------------------------------------------ MCP paths ---

def _mcp_db(tmp_path):
    from memorymaster.surfaces.mcp_server import _project_scope

    workspace = tmp_path / "ws"
    workspace.mkdir()
    db, ids = build_db(tmp_path, TEXTS, scope=_project_scope(str(workspace)), name="mcp.db")
    return db, ids, workspace


def test_mcp_query_for_context_off_matches_service_and_live_selects(tmp_path, monkeypatch):
    from memorymaster.surfaces.mcp_server import _effective_scope_allowlist, query_for_context

    db, _, workspace = _mcp_db(tmp_path)
    args = dict(query="zebracache", db=str(db), workspace=str(workspace), retrieval_mode="legacy")
    off = query_for_context(**args)
    direct = MemoryService(db, workspace_root=workspace, read_only=True).query_for_context(
        query="zebracache", token_budget=4000, output_format="text", limit=100, include_stale=None,
        include_conflicted=None, include_candidates=None, retrieval_mode="legacy", trust_mode="trusted",
        allow_sensitive=False, scope_allowlist=_effective_scope_allowlist("", str(workspace)))
    assert off["output"] == direct.output and off["claims_included"] == direct.claims_included
    assert not (tmp_path / "decisions.db").exists()

    legacy_ids = [row["claim"].id for row in direct.rows]
    plan = {cid: {"relevant": 0.1, "usable": 0.1} for cid in legacy_ids}
    plan[legacy_ids[-1]] = {"relevant": 0.95, "usable": 0.9, "conflict": 0.95}
    plan[legacy_ids[0]] = {"relevant": 0.8, "usable": 0.9}
    transport = ScriptedTransport(recall_answers(plan))
    install_engine(monkeypatch, tmp_path, transport)

    live = query_for_context(**args, detail_level="summary")

    assert [claim["claim_id"] for claim in live["claims"]] == [legacy_ids[-1], legacy_ids[0]]
    assert live["claims_included"] == 2
    assert jev.LABEL_CONFLICT in live["output"]
    assert live["output"].index(f"id={legacy_ids[-1]} ") < live["output"].index(f"id={legacy_ids[0]} ")
    decision = ledger_rows(tmp_path, "SELECT * FROM decisions")[0]
    assert json.loads(decision["baseline_features_json"])["path"] == "mcp_query_for_context"
    items = ledger_rows(tmp_path, "SELECT DISTINCT item_ref FROM decision_items WHERE exposed = 1 AND delivered = 1")
    assert {row["item_ref"] for row in items} == {f"claim:{legacy_ids[-1]}", f"claim:{legacy_ids[0]}"}


def test_mcp_query_for_context_with_allow_sensitive_is_not_sent(tmp_path, monkeypatch):
    from memorymaster.surfaces import mcp_server

    db, _, workspace = _mcp_db(tmp_path)
    transport = ScriptedTransport(recall_answers({}))
    install_engine(monkeypatch, tmp_path, transport)
    monkeypatch.setattr(mcp_server, "resolve_allow_sensitive_access", lambda **_kw: None)
    mcp_server.query_for_context(query="zebracache", db=str(db), workspace=str(workspace),
                                 retrieval_mode="legacy", allow_sensitive=True)
    assert transport.calls == []


def test_mcp_recall_off_is_unchanged_and_live_rewrites_claims_section(tmp_path, monkeypatch):
    from memorymaster.surfaces.mcp_server import recall as mcp_recall

    db, _, workspace = _mcp_db(tmp_path)
    args = dict(query="zebracache", db=str(db), workspace=str(workspace), retrieval_mode="legacy",
                session_id=SESSION)
    off = mcp_recall(**args)
    assert off["claims"] and not (tmp_path / "decisions.db").exists()
    from memorymaster.public.v1 import recall as public_recall
    from memorymaster.surfaces.mcp_server import _effective_scope_allowlist

    direct = public_recall("zebracache", scope_allowlist=_effective_scope_allowlist("", str(workspace)),
                           retrieval_mode="legacy", session_id=SESSION, source_agent="memorymaster-mcp",
                           platform="mcp", db=str(db), workspace=str(workspace))
    assert off["output"] == direct.output and [c["claim_id"] for c in off["claims"]] == [
        c["claim_id"] for c in direct.claims]
    legacy_ids = [claim["claim_id"] for claim in off["claims"]]

    plan = {cid: {"relevant": 0.1, "usable": 0.1} for cid in legacy_ids}
    plan[legacy_ids[-1]] = {"relevant": 0.95, "usable": 0.9, "instruction": 0.9}
    plan[legacy_ids[-2]] = {"relevant": 0.85, "usable": 0.9}
    plan[legacy_ids[-3]] = {"relevant": 0.75, "usable": 0.9}
    install_engine(monkeypatch, tmp_path, ScriptedTransport(recall_answers(plan)))

    live = mcp_recall(**args)

    assert [claim["claim_id"] for claim in live["claims"]] == [legacy_ids[-1], legacy_ids[-2], legacy_ids[-3]]
    assert live["output"].startswith("# Relevant Memory Claims")
    assert jev.LABEL_INSTRUCTION in live["output"] and "3/3 claims" in live["output"]
    assert live["tokens_used"] < off["tokens_used"]
    decision = ledger_rows(tmp_path, "SELECT * FROM decisions")[0]
    assert decision["session_key"] == jev.decision_session_key(SESSION)
    assert json.loads(decision["baseline_features_json"])["path"] == "mcp_recall"


def test_label_helpers():
    assert jev.project_label(cwd=str(Path("a") / "b" / "proj"), scope="project:x") == "proj"
    assert jev.project_label(cwd=None, scope="project:memorymaster") == "memorymaster"
    assert jev.project_label(cwd=str(Path.home()), scope="project:fallback") == "fallback"


# ----------------------------------------- MCP egress policy (B1) ---

MIXED = [
    ("zebracache PRIVATEMARKER note that belongs to agent-b alone", "private", "agent-b"),
    ("zebracache SENSITIVEMARKER note kept out of shared views", "sensitive", "agent-d"),
]


def _mcp_mixed_db(tmp_path):
    """The MCP workspace fixtures plus a confirmed private and a confirmed sensitive claim."""
    from memorymaster.surfaces.mcp_server import _project_scope

    db, public_ids, workspace = _mcp_db(tmp_path)
    service = MemoryService(db, workspace_root=workspace)
    for index, (text, visibility, agent) in enumerate(MIXED):
        claim = service.ingest(text, [CitationInput(source=f"test://mixed-{index}", locator="fixture")],
                               scope=_project_scope(str(workspace)), visibility=visibility, source_agent=agent)
        transition_claim(service.store, claim.id, "confirmed", "test fixture")
    return db, public_ids, workspace


@pytest.mark.parametrize("mode", ["shadow", "live"])
@pytest.mark.parametrize("tool", ["query_for_context", "recall"])
def test_mcp_paths_never_send_private_or_sensitive_claims(tmp_path, monkeypatch, tool, mode):
    from memorymaster.surfaces import mcp_server

    db, public_ids, workspace = _mcp_mixed_db(tmp_path)
    call = getattr(mcp_server, tool)
    args = dict(query="zebracache", db=str(db), workspace=str(workspace), retrieval_mode="legacy")
    legacy = call(**args)
    # the caller is authorized to read both: only their egress is in question
    assert "PRIVATEMARKER" in legacy["output"] and "SENSITIVEMARKER" in legacy["output"]
    plan = {cid: {"relevant": 0.9 - 0.1 * n, "usable": 0.9} for n, cid in enumerate(public_ids)}
    transport = ScriptedTransport(recall_answers(plan))
    install_engine(monkeypatch, tmp_path, transport, MEMORYMASTER_JEV_MODE=mode)

    out = call(**args)

    assert len(transport.calls) == 1
    sent = json.dumps(transport.calls[0]["payload"])
    assert "PRIVATEMARKER" not in sent and "SENSITIVEMARKER" not in sent
    asked = {wire.split("::", 1)[1] for wire in transport.calls[0]["payload"]["questions"]}
    assert asked == {f"claim:{cid}" for cid in public_ids}
    decision = ledger_rows(tmp_path, "SELECT * FROM decisions")[0]
    assert decision["fallback_reason"] is None and decision["mode"] == mode
    assert "PRIVATEMARKER" not in decision["state_redacted"] and "SENSITIVEMARKER" not in decision["state_redacted"]
    assert json.loads(decision["baseline_features_json"])["passthrough"] == 2
    if mode == "shadow":
        assert out["output"] == legacy["output"]
    else:  # Jev only chose among the public candidates; the authorized rest passes through
        assert set(json.loads(decision["action_taken"])) <= {f"claim:{cid}" for cid in public_ids}
        assert "PRIVATEMARKER" in out["output"] and "SENSITIVEMARKER" in out["output"]


# ------------------------------------ B1 passthrough keeps legacy positions ---

def _mixed_rows(tmp_path):
    """Rows in a known legacy order: public A, private, public B, sensitive, public C, public D."""
    from memorymaster.recall.context_optimizer import pack_context

    db, public_ids, workspace = _mcp_mixed_db(tmp_path)
    service = MemoryService(db, workspace_root=workspace, read_only=True)
    claims = {claim.id: claim for claim in service.store.list_claims(limit=50, include_citations=True)}
    private = next(cid for cid, claim in claims.items() if "PRIVATEMARKER" in claim.text)
    sensitive = next(cid for cid, claim in claims.items() if "SENSITIVEMARKER" in claim.text)
    order = [public_ids[0], private, public_ids[1], sensitive, public_ids[2], public_ids[3]]
    rows = [{"claim": claims[cid], "score": 1.0 - 0.1 * n} for n, cid in enumerate(order)]
    legacy = pack_context(rows, token_budget=4000, output_format="text")
    assert [row["claim"].id for row in legacy.rows] == order
    return service, legacy, order


def _pick(first, second, others):
    plan = {cid: {"relevant": 0.1, "usable": 0.1} for cid in others}
    plan[first] = {"relevant": 0.95, "usable": 0.9}
    plan[second] = {"relevant": 0.85, "usable": 0.9}
    return plan


def test_query_for_context_live_keeps_authorized_private_rows_in_their_legacy_positions(tmp_path, monkeypatch):
    service, legacy, order = _mixed_rows(tmp_path)
    pub_a, private, pub_b, sensitive, pub_c, pub_d = order
    transport = ScriptedTransport(recall_answers(_pick(pub_d, pub_a, order)))
    install_engine(monkeypatch, tmp_path, transport)

    live = jev.apply_recall_to_context(legacy, query="zebracache", token_budget=4000, output_format="text",
                                       project="ws")

    assert [row["claim"].id for row in live.rows] == [pub_d, private, pub_a, sensitive]
    assert "PRIVATEMARKER" in live.output and "SENSITIVEMARKER" in live.output
    assert live.claims_included == 4
    assert live.output.index("PRIVATEMARKER") < live.output.index(f"id={pub_a} ")
    sent = json.dumps(transport.calls[0]["payload"])
    assert "PRIVATEMARKER" not in sent and "SENSITIVEMARKER" not in sent
    assert {wire.split("::", 1)[1] for wire in transport.calls[0]["payload"]["questions"]} == {
        f"claim:{cid}" for cid in (pub_a, pub_b, pub_c, pub_d)}
    decision = ledger_rows(tmp_path, "SELECT * FROM decisions")[0]
    assert json.loads(decision["baseline_features_json"])["passthrough"] == 2
    assert json.loads(decision["action_taken"]) == [f"claim:{pub_d}", f"claim:{pub_a}"]
    assert json.loads(decision["legacy_action"]) == [f"claim:{cid}" for cid in (pub_a, pub_b, pub_c, pub_d)]


def test_recall_receipt_live_keeps_authorized_private_rows_in_their_legacy_positions(tmp_path, monkeypatch):
    from memorymaster.public.v1 import RecallReceipt, _recall_claim

    service, legacy, order = _mixed_rows(tmp_path)
    pub_a, private, pub_b, sensitive, pub_c, pub_d = order
    receipt = RecallReceipt(api_version="v1", output=legacy.output + "\n\n## Skills\n- untouched",
                            claims=tuple(_recall_claim(row) for row in legacy.rows), token_budget=4000,
                            tokens_used=legacy.tokens_used, trust_mode="trusted", output_format="text")
    transport = ScriptedTransport(recall_answers(_pick(pub_c, pub_b, order)))
    install_engine(monkeypatch, tmp_path, transport)

    live = jev.apply_recall_to_receipt(
        receipt, query="zebracache", project="ws",
        fetch_claim=lambda cid: service.store.get_claim(cid, include_citations=True))

    assert [entry["claim_id"] for entry in live.claims] == [pub_c, private, pub_b, sensitive]
    assert "PRIVATEMARKER" in live.output and "SENSITIVEMARKER" in live.output
    assert live.output.endswith("\n\n## Skills\n- untouched") and "4/4 claims" in live.output
    sent = json.dumps(transport.calls[0]["payload"])
    assert "PRIVATEMARKER" not in sent and "SENSITIVEMARKER" not in sent
    decision = ledger_rows(tmp_path, "SELECT * FROM decisions")[0]
    assert json.loads(decision["baseline_features_json"])["passthrough"] == 2


def test_passthrough_rows_that_leave_no_room_keep_the_legacy_output(tmp_path, monkeypatch):
    service, legacy, order = _mixed_rows(tmp_path)
    transport = ScriptedTransport(recall_answers(_pick(order[5], order[0], order)))
    install_engine(monkeypatch, tmp_path, transport)
    from memorymaster.recall.context_optimizer import pack_context

    # A budget that holds exactly the two passthrough rows: no flagged public row
    # fits beside them, so Jev is not asked and the legacy output stays.
    fixed = [row for row in legacy.rows if row["claim"].id in (order[1], order[3])]
    tight = pack_context(fixed, token_budget=4000, output_format="text").tokens_used
    assert jev.pack_cap([row for row in legacy.rows if row not in fixed], tight, "text", fixed=fixed) == 0

    live = jev.apply_recall_to_context(legacy, query="zebracache", token_budget=tight, output_format="text",
                                       project="ws")

    assert live is legacy
    assert transport.calls == []


def test_merge_passthrough_keeps_legacy_slots_and_appends_the_overflow():
    assert jev.merge_passthrough(["a", "b"], [(1, "P"), (3, "Q")]) == ["a", "P", "b", "Q"]
    assert jev.merge_passthrough(["a"], [(0, "P"), (5, "Q")]) == ["P", "a", "Q"]
    assert jev.merge_passthrough([], [(2, "P")]) == ["P"]
    assert jev.merge_passthrough(["a", "b"], []) == ["a", "b"]


# ------------------------------------- S7 inside one recall (B2) ---

ROUTE_SITES = {"MEMORYMASTER_RECALL_FUSION": "auto", "MEMORYMASTER_RECALL_W_CLAIM_TYPE": "0.2"}


def with_route(answer, route_choice="temporal"):
    """``answer`` for the recall questions plus a confident S7 Choice."""

    def combined(wire_id, schema):
        if schema.primitive != "choice":
            return answer(wire_id, schema)
        options = list(schema.options)
        rest = 0.1 / (len(options) - 1)
        probabilities = {option: (0.9 if option == route_choice else rest) for option in options}
        return ParsedAnswer("choice", route_choice, probabilities, None, route_choice)

    return combined


def surfaces_requested(transport):
    return [next(iter(call["payload"]["questions"])).split("::", 1)[0].split(".", 1)[0]
            for call in transport.calls]


def _route_sites_on(monkeypatch):
    for name, value in ROUTE_SITES.items():
        monkeypatch.setenv(name, value)


def test_route_runs_at_most_once_per_recall(tmp_path, monkeypatch):
    db, _ = build_db(tmp_path, TEXTS)
    _route_sites_on(monkeypatch)
    transport = ScriptedTransport(with_route(recall_answers({})))
    install_engine(monkeypatch, tmp_path, transport, MEMORYMASTER_JEV_MODE="off", MEMORYMASTER_JEV_ROUTE="live")

    context_hook.recall(QUERY, db_path=str(db), skip_qdrant=True)

    assert surfaces_requested(transport) == ["route"]
    assert [row["surface"] for row in ledger_rows(tmp_path, "SELECT surface FROM decisions")] == ["route"]


def test_prompt_hook_route_carries_session_and_scope_when_recall_is_off(tmp_path, monkeypatch):
    db, _ = build_db(tmp_path, TEXTS)
    _route_sites_on(monkeypatch)
    transport = ScriptedTransport(with_route(recall_answers({})))
    install_engine(monkeypatch, tmp_path, transport, MEMORYMASTER_JEV_MODE="off", MEMORYMASTER_JEV_ROUTE="live")

    context_hook.recall(QUERY, db_path=str(db), skip_qdrant=True, hook_data=hook_data(tmp_path))

    rows = ledger_rows(tmp_path, "SELECT surface, session_key, scope FROM decisions")
    assert [(row["surface"], row["session_key"], row["scope"]) for row in rows] == [
        ("route", jev.decision_session_key(SESSION), "project:allowed")]


def test_prompt_hook_sends_one_request_when_recall_and_route_are_live(tmp_path, monkeypatch):
    db, _ = build_db(tmp_path, TEXTS)
    _route_sites_on(monkeypatch)
    _, legacy_ids = legacy_recall(db)
    plan = {cid: {"relevant": 0.9 - 0.1 * n, "usable": 0.9} for n, cid in enumerate(legacy_ids)}
    transport = ScriptedTransport(with_route(recall_answers(plan)))
    install_engine(monkeypatch, tmp_path, transport)

    context_hook.recall(QUERY, db_path=str(db), skip_qdrant=True, hook_data=hook_data(tmp_path))

    assert surfaces_requested(transport) == ["recall"]
    assert [row["surface"] for row in ledger_rows(tmp_path, "SELECT surface FROM decisions")] == ["recall"]


def test_prompt_hook_jev_time_stays_within_one_hook_deadline(tmp_path, monkeypatch):
    db, _ = build_db(tmp_path, TEXTS)
    _route_sites_on(monkeypatch)
    data = hook_data(tmp_path)
    context_hook.recall(QUERY, db_path=str(db), skip_qdrant=True, hook_data=data)  # warm imports
    started = time.perf_counter()
    legacy_out = context_hook.recall(QUERY, db_path=str(db), skip_qdrant=True, hook_data=data)
    baseline = time.perf_counter() - started
    transport = ScriptedTransport(with_route(recall_answers({})), delay=3.0)
    install_engine(monkeypatch, tmp_path, transport, MEMORYMASTER_JEV_HOOK_DEADLINE_MS="900")  # production value

    started = time.perf_counter()
    out = context_hook.recall(QUERY, db_path=str(db), skip_qdrant=True, hook_data=data)
    elapsed = time.perf_counter() - started

    assert out == legacy_out
    assert len(transport.calls) == 1
    # one 900 ms deadline plus the engine's late grace and ledger setup; never two or three deadlines
    assert elapsed - baseline < 0.9 + 0.05 + 0.6
    rows = ledger_rows(tmp_path, "SELECT surface, fallback_reason FROM decisions")
    assert [(row["surface"], row["fallback_reason"]) for row in rows] == [("recall", "timeout")]


# ----------------------------- delivery prediction past the pool ---

def test_shadow_delivery_prediction_covers_legacy_blocks_longer_than_the_pool(tmp_path, monkeypatch):
    from types import SimpleNamespace

    rows = [{"claim": SimpleNamespace(id=cid, text=f"zebracache note number {cid}", wiki_article=None)}
            for cid in range(1, 26)]
    budget = 10_000
    lines, rendered = context_hook._render_recall_lines(rows, budget)
    assert len(rendered) == 25 > jev.RECALL_TOP_N
    data = hook_data(tmp_path)
    # the same block was delivered moments ago: this repeat is suppressed
    assert delivery.deliver(data, delivery.recall_block(context_hook._recall_text(lines)), lambda _output: None)
    plan = {1: {"relevant": 0.9, "usable": 0.9, "conflict": 0.9}}
    install_engine(monkeypatch, tmp_path, ScriptedTransport(recall_answers(plan)), MEMORYMASTER_JEV_MODE="shadow")

    assert context_hook._jev_recall_rendering(QUERY, rows, lines, rendered, budget, data, {}) is None

    items = ledger_rows(tmp_path, "SELECT item_ref, MAX(delivered) AS d FROM decision_items WHERE exposed = 1 "
                                  "GROUP BY item_ref")
    assert len(items) == 25 and all(row["d"] == 0 for row in items)


def test_an_active_surface_with_nothing_to_ask_leaves_a_skip_row(tmp_path, monkeypatch):
    """'Everything logged' covers the contexts where S2 or S8 is on but asks nothing:
    one ``skip:`` row with the legacy action, no request (keeps OPE honest)."""
    transport = ScriptedTransport(recall_answers({}))
    install_engine(monkeypatch, tmp_path, transport)

    assert jev.decide_recall("which memory", [], legacy_ids=[7, 8], project_label="demo", k_cap=5) is None
    assert jev.decide_session("demo", [jev.RecallCandidate(9, "a public claim")], legacy_ids=[1, 2, 3, 4, 5],
                              passthrough_ids=[1, 2, 3, 4, 5]) is None

    assert transport.calls == []
    rows = ledger_rows(tmp_path, "SELECT surface, fallback_reason, attempt_count, action_taken FROM decisions "
                                 "ORDER BY ts")
    assert [(r["surface"], r["fallback_reason"], r["attempt_count"]) for r in rows] == [
        ("recall", "skip:no_candidates", 0), ("session", "skip:all_passthrough", 0)]
    assert json.loads(rows[0]["action_taken"]) == ["claim:7", "claim:8"]


def test_a_session_skip_row_never_logs_the_private_passthrough_ids(tmp_path, monkeypatch):
    """Review of 025d40b: the live S8 path never logs passthrough (private/sensitive) ids,
    so its skip row must not either."""
    install_engine(monkeypatch, tmp_path, ScriptedTransport(recall_answers({})))
    assert jev.decide_session("demo", [jev.RecallCandidate(9, "a public claim")], legacy_ids=[1, 2, 3, 4, 5],
                              passthrough_ids=[1, 2, 3, 4, 5]) is None
    [row] = ledger_rows(tmp_path, "SELECT action_taken FROM decisions WHERE surface = 'session'")
    assert json.loads(row["action_taken"]) == []
    assert ledger_rows(tmp_path, "SELECT item_ref FROM decision_items") == []

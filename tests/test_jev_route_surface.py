"""S7 ROUTE: Jev chooses the query type where recall auto-classifies; keyword rules otherwise.

Scripted in-process transport and a temp decisions ledger: nothing leaves the machine.
"""
from __future__ import annotations

import os

import pytest

from memorymaster.decisions import engine as decisions_engine
from memorymaster.decisions.config import DecisionConfig
from memorymaster.decisions.credentials import ApiKey
from memorymaster.decisions.engine import DecisionEngine
from memorymaster.decisions.ledger import DecisionLedger
from memorymaster.decisions.transport import ParsedAnswer, TransportResult
from memorymaster.recall import context_hook
from memorymaster.recall.query_classifier import classify_query, route_query

KEY = ApiKey("ts-test-" + "Q" * 24)  # synthetic


class ScriptedTransport:
    def __init__(self, answer=None, *, outcome: str = "ok"):
        self.answer = answer
        self.outcome = outcome
        self.calls: list[dict] = []

    def send(self, payload, *, expected, timeout_s, max_retries=0):
        self.calls.append({"payload": payload, "timeout_s": timeout_s, "max_retries": max_retries})
        if self.outcome != "ok":
            return TransportResult(None, None, 12, 1, self.outcome)
        answers = {wire_id: self.answer(wire_id, schema) for wire_id, schema in expected.items()}
        return TransportResult(200, {"model": "jev-1.13.0"}, 40, 1, "ok", model_served="jev-1.13.0",
                               tokens_in=80, tokens_out=0, cost_usd=80 * 0.042e-6, answers=answers)

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith(("MEMORYMASTER_JEV", "MEMORYMASTER_DECISIONS", "TYPESAFE_")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MEMORYMASTER_DECISIONS_DB", str(tmp_path / "decisions.db"))


def install_engine(monkeypatch, tmp_path, transport, **env):
    monkeypatch.setenv("MEMORYMASTER_JEV_MODE", "live")
    # Not deadline tests: a hook deadline no loaded CI runner reaches (the 900 ms default
    # made recall-surface tests fall back on Ubuntu CI, 2026-09-24).
    monkeypatch.setenv("MEMORYMASTER_JEV_HOOK_DEADLINE_MS", "10000")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    engine = DecisionEngine(DecisionConfig.from_env(), transport_factory=lambda _key: transport,
                            key_lookup=lambda: KEY)
    monkeypatch.setattr(decisions_engine, "default_engine", lambda: engine)
    return engine


def ledger_rows(tmp_path, sql):
    return DecisionLedger(tmp_path / "decisions.db").query(sql)


def route_answers(choice: str, p: float):
    def answer(wire_id, schema):
        assert schema.primitive == "choice"
        rest = (1.0 - p) / (len(schema.options) - 1)
        probabilities = {option: (p if option == choice else rest) for option in schema.options}
        return ParsedAnswer("choice", choice, probabilities, None, choice)

    return answer


def test_route_off_uses_keyword_rules_without_a_request(tmp_path, monkeypatch):
    monkeypatch.setattr(decisions_engine, "default_engine", lambda: pytest.fail("engine used while off"))
    assert route_query("what depends on the ledger?") == classify_query("what depends on the ledger?")


@pytest.mark.parametrize(("choice", "p", "expected"), [
    ("relational", 0.9, "relational"),
    ("unknown", 0.95, None),
    ("temporal", 0.4, None),
])
def test_route_live_choice_with_keyword_fallback(tmp_path, monkeypatch, choice, p, expected):
    query = "Tell me about the ledger design"
    transport = ScriptedTransport(route_answers(choice, p))
    install_engine(monkeypatch, tmp_path, transport)
    assert route_query(query) == (expected or classify_query(query))
    decision = ledger_rows(tmp_path, "SELECT * FROM decisions")[0]
    assert decision["surface"] == "route" and decision["mode"] == "live"
    options = transport.calls[0]["payload"]["questions"]["route.query_type"]["criteria"]
    assert set(options) == {"fact_lookup", "relational", "temporal", "constraint_check", "preference",
                            "verification", "open_ended", "unknown"}


def test_route_failure_falls_back_to_keyword_rules(tmp_path, monkeypatch):
    install_engine(monkeypatch, tmp_path, ScriptedTransport(outcome="network_error"))
    assert route_query("is it true that the hook is quiet?") == "verification"
    assert ledger_rows(tmp_path, "SELECT fallback_reason FROM decisions")[0]["fallback_reason"] == "network_error"


def test_recall_auto_classification_goes_through_route(tmp_path, monkeypatch):
    install_engine(monkeypatch, tmp_path, ScriptedTransport(route_answers("temporal", 0.9)))
    assert classify_query("what is the ledger") == "fact_lookup"
    assert context_hook._classify_query_type("what is the ledger") == "temporal"


def test_route_prompt_path_uses_hook_kind(tmp_path, monkeypatch):
    transport = ScriptedTransport(route_answers("preference", 0.9))
    install_engine(monkeypatch, tmp_path, transport)
    route_query("how should replies be formatted")
    # hook kind: no retries (batch retries 3 times), at most the time left of the hook deadline
    assert transport.calls[0]["max_retries"] == 0 and 0 < transport.calls[0]["timeout_s"] <= 10.0

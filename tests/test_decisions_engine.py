"""Decision engine: modes, fallbacks, deadline, late answers, exploration and full ledger rows."""
from __future__ import annotations

import http.server
import json
import math
import os
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from memorymaster.decisions import engine as eng
from memorymaster.decisions import policy as pl
from memorymaster.decisions import questions as q
from memorymaster.decisions.config import DecisionConfig
from memorymaster.decisions.credentials import ApiKey
from memorymaster.decisions.engine import DecisionContext, DecisionEngine, DecisionItem, JevChoice
from memorymaster.decisions.ledger import DecisionLedger, DecisionRecord, utc_iso
from memorymaster.decisions.transport import HttpTransport, ParsedAnswer, TransportResult

REPO = Path(__file__).resolve().parents[1]
KEY = ApiKey("ts-test-" + "E" * 24)  # synthetic


def answer_for(wire_id: str, schema) -> ParsedAnswer:
    """Deterministic fake answers: claim:1 is best, claim:3 worst."""
    rank = {"claim:1": 0.9, "claim:2": 0.6, "claim:3": 0.2}
    ref = wire_id.split("::", 1)[1] if "::" in wire_id else ""
    value = rank.get(ref, 0.8)
    if schema.primitive == "noul":
        return ParsedAnswer("noul", value, {"noul": value})
    if schema.primitive == "score":
        levels = [str(level) for level in schema.levels]
        probs = {level: 0.0 for level in levels}
        probs[levels[-1]] = value
        probs[levels[0]] = 1.0 - value
        return ParsedAnswer("score", value, probs, None, levels[-1] if value >= 0.5 else levels[0])
    options = list(schema.options)
    probs = {option: (1.0 if i == 0 else 0.0) for i, option in enumerate(options)}
    return ParsedAnswer("choice", options[0], probs, 0.9, options[0])


class FakeTransport:
    def __init__(self, outcome: str = "ok", delay: float = 0.0, latency_ms: int = 42):
        self.outcome = outcome
        self.delay = delay
        self.latency_ms = latency_ms
        self.calls: list[dict] = []

    def send(self, payload, *, expected, timeout_s, max_retries=0):
        self.calls.append({"payload": payload, "expected": expected, "timeout_s": timeout_s,
                           "max_retries": max_retries})
        if self.delay:
            time.sleep(self.delay)
        if self.outcome != "ok":
            return TransportResult(403 if self.outcome == "http_403" else None, None, self.latency_ms, 1, self.outcome)
        answers = {wire_id: answer_for(wire_id, schema) for wire_id, schema in expected.items()}
        return TransportResult(200, {"model": "jev-1.13.0"}, self.latency_ms, 1, "ok", model_served="jev-1.13.0",
                               tokens_in=500, tokens_out=3, cost_usd=500 * 0.042e-6, answers=answers)

    def close(self):
        pass


def recall_choose(answers) -> JevChoice:
    refs = sorted(answers.item_refs(), key=lambda r: -(answers.score("recall.relevant", r) or 0.0))
    k = max(2, min(5, sum(1 for r in refs if (answers.noul("recall.usable_evidence", r) or 0) >= 0.5)))
    return JevChoice(order=refs, k=k, scores={r: answers.score("recall.relevant", r) or 0.0 for r in refs})


ITEMS = [DecisionItem("claim:3", rank_legacy=1), DecisionItem("claim:2", rank_legacy=2),
         DecisionItem("claim:1", rank_legacy=3)]
LEGACY = ["claim:3", "claim:2"]


def recall_inputs(query: str = "how do we run tests on 10.0.0.5?"):
    state, bound = q.build_recall(query, "memorymaster",
                                  [("claim:3", "old note"), ("claim:2", "Use WAL"), ("claim:1", "Run pytest")])
    return state, bound


# MEMORYMASTER_SKIP_PERF (CI): shared runners swing ~5x, so only wall-clock limits are skipped.
TIMING = not os.environ.get("MEMORYMASTER_SKIP_PERF")
# Tests get a hook deadline no loaded machine reaches unless they are about the deadline: at the
# 900 ms production default, Windows CI (2026-09-24) timed out a label test and a loaded release
# machine timed out the surrogate and send-intent tests. Deadline tests pass their own value.
UNTIMED_HOOK_S = 10.0
PRODUCTION_HOOK = {"MEMORYMASTER_JEV_HOOK_DEADLINE_MS": "900"}


def make_engine(tmp_path, *, mode="live", transport=None, key=KEY, env=None, ids=None, **kwargs):
    environ = {"MEMORYMASTER_JEV_MODE": mode, "MEMORYMASTER_DECISIONS_DB": str(tmp_path / "decisions.db"),
               "MEMORYMASTER_JEV_HOOK_DEADLINE_MS": str(int(UNTIMED_HOOK_S * 1000))}
    environ.update(env or {})
    config = DecisionConfig.from_env(environ)
    fake = transport or FakeTransport()
    counter = iter(ids or [f"dec-{n}" for n in range(1000)])
    engine = DecisionEngine(config, transport_factory=lambda _key: fake, key_lookup=lambda: key,
                            id_factory=lambda: next(counter), **kwargs)
    return engine, fake


def decide_recall(engine, **ctx):
    state, bound = recall_inputs()
    return engine.decide("recall", state=state, questions=bound, items=ITEMS, legacy_action=LEGACY,
                         choose=recall_choose, context=DecisionContext(session_key="sess-1", scope="project:mm",
                                                                        legacy_exposed=LEGACY, **ctx))


def rows(tmp_path, sql):
    return DecisionLedger(tmp_path / "decisions.db").query(sql)


def test_off_mode_sends_nothing_and_writes_nothing(tmp_path):
    engine, fake = make_engine(tmp_path, mode="off")
    decision = decide_recall(engine)
    assert decision.action == LEGACY and decision.mode == "off" and decision.fallback_reason == "mode_off"
    assert fake.calls == []
    assert not (tmp_path / "decisions.db").exists()


def test_off_mode_logs_minimal_row_when_requested(tmp_path):
    engine, fake = make_engine(tmp_path, mode="off", env={"MEMORYMASTER_DECISIONS_LOG_OFF": "1"})
    decision = decide_recall(engine)
    assert fake.calls == []
    row = rows(tmp_path, "SELECT * FROM decisions")[0]
    assert row["mode"] == "off" and row["fallback_reason"] == "mode_off" and row["decision_id"] == decision.decision_id
    assert row["state_redacted"] is None and row["attempt_count"] == 0
    exposed = rows(tmp_path, "SELECT item_ref FROM decision_items WHERE exposed = 1 ORDER BY item_ref")
    assert [r["item_ref"] for r in exposed] == ["claim:2", "claim:3"]


def test_off_mode_in_fresh_interpreter_imports_no_http_client(tmp_path):
    script = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(REPO)!r})
        from memorymaster.decisions import engine, questions
        state, bound = questions.build_hints("we decided to use WAL")
        d = engine.decide("hints", state=state, questions=bound, items=[], legacy_action=["decision"],
                          choose=lambda a: engine.JevChoice(action=["decision"]))
        print(d.mode, d.fallback_reason, "httpx" in sys.modules, "requests" in sys.modules)
        """
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MEMORYMASTER_", "TYPESAFE_"))}
    env["MEMORYMASTER_DECISIONS_DB"] = str(tmp_path / "never.db")
    out = subprocess.run([sys.executable, "-I", "-c", script], cwd=str(tmp_path), env=env,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["off", "mode_off", "False", "False"]
    assert not (tmp_path / "never.db").exists()


def test_live_recall_acts_on_jev_and_logs_complete_rows(tmp_path):
    delivered_seen = []

    def deliver(refs):
        delivered_seen.append(list(refs))
        return [r for r in refs if r != "claim:2"]  # delivery suppression drops one

    engine, fake = make_engine(tmp_path, env={"MEMORYMASTER_JEV_EXPLORE_RECALL": "0"})
    decision = decide_recall(engine, delivery_filter=deliver, baseline_features={"bm25": [1, 2, 3]})
    assert decision.mode == "live" and decision.fallback_reason is None
    assert decision.action == ["claim:1", "claim:2"]
    assert decision.items == ["claim:1"]
    assert delivered_seen == [["claim:1", "claim:2"]]
    assert len(fake.calls) == 1 and fake.calls[0]["max_retries"] == 0
    assert 0 < fake.calls[0]["timeout_s"] <= UNTIMED_HOOK_S  # the hook deadline, less the engine time spent
    row = rows(tmp_path, "SELECT * FROM decisions")[0]
    for column in ("ts", "policy_version", "question_set_id", "question_sha256", "primitive_summary",
                   "model_requested", "model_served", "backend", "transport_version", "sdk_version", "code_revision",
                   "state_sha256", "state_redacted", "egress_bytes", "redaction_counts_json", "latency_ms",
                   "attempt_count", "tokens_in", "tokens_out", "cost_usd", "legacy_action", "jev_action",
                   "available_actions_json", "action_taken", "exploration_arm", "action_propensities_json",
                   "chosen_propensity", "randomization_id", "thresholds_json", "baseline_features_json"):
        assert row[column] is not None, column
    assert row["transport_outcome"] == "ok" and row["fallback_reason"] is None
    assert row["model_served"] == "jev-1.13.0" and row["tokens_in"] == 500
    assert json.loads(row["action_taken"]) == decision.action
    assert json.loads(row["legacy_action"]) == LEGACY
    assert row["session_key"] == "sess-1" and row["scope"] == "project:mm"
    assert "10.0.0.5" not in row["state_redacted"]
    assert json.loads(row["redaction_counts_json"]) == {"private_ip": 1}
    propensities = json.loads(row["action_propensities_json"])
    assert math.isclose(sum(p["p"] for p in propensities), 1.0)
    assert row["chosen_propensity"] == pytest.approx(1.0)
    thresholds = json.loads(row["thresholds_json"])
    assert thresholds["recall.usable_evidence@v1"]["include"] == 0.5
    items = rows(tmp_path, "SELECT * FROM decision_items WHERE question_id = 'recall.usable_evidence' ORDER BY item_ref")
    assert [(r["item_ref"], r["rank_legacy"], r["rank_final"], r["exposed"], r["delivered"]) for r in items] == [
        ("claim:1", 3, 1, 1, 1), ("claim:2", 2, 2, 1, 0), ("claim:3", 1, 3, 0, 0)]
    assert json.loads(items[0]["probabilities_json"]) == {"noul": 0.9}
    assert len(rows(tmp_path, "SELECT * FROM decision_items")) == 12
    versions = rows(tmp_path, "SELECT question_id FROM question_versions")
    assert {r["question_id"] for r in versions} >= {"recall.relevant", "recall.usable_evidence"}


def test_payload_sent_is_redacted_and_candidates_stay_in_their_questions(tmp_path):
    engine, fake = make_engine(tmp_path)
    decide_recall(engine)
    payload = fake.calls[0]["payload"]
    assert payload["model"] == "jev-1.13.0"
    assert "10.0.0.5" not in json.dumps(payload)
    assert payload["state"] == {"request": "how do we run tests on [REDACTED:private_ip]?", "project": "memorymaster"}
    wire = payload["questions"]["recall.usable_evidence::claim:2"]
    assert wire["instructions"]["memory"] == "Use WAL"
    assert "Run pytest" not in json.dumps(wire)


def test_shadow_returns_legacy_and_logs_jev_action(tmp_path):
    engine, fake = make_engine(tmp_path, mode="shadow")
    decision = decide_recall(engine)
    assert decision.action == LEGACY and decision.mode == "shadow" and decision.fallback_reason is None
    assert decision.jev_action == ["claim:1", "claim:2"]
    assert len(fake.calls) == 1
    row = rows(tmp_path, "SELECT * FROM decisions")[0]
    assert json.loads(row["action_taken"]) == LEGACY and json.loads(row["jev_action"]) == ["claim:1", "claim:2"]
    assert row["exploration_arm"] == "shadow" and row["chosen_propensity"] == 1.0
    exposed = rows(tmp_path, "SELECT DISTINCT item_ref FROM decision_items WHERE exposed = 1 ORDER BY item_ref")
    assert [r["item_ref"] for r in exposed] == ["claim:2", "claim:3"]


def test_missing_key_falls_back_without_sending(tmp_path):
    engine, fake = make_engine(tmp_path, key=None)
    decision = decide_recall(engine)
    assert decision.action == LEGACY and decision.fallback_reason == "missing_key"
    assert fake.calls == []
    row = rows(tmp_path, "SELECT * FROM decisions")[0]
    assert row["transport_outcome"] == "not_sent" and row["attempt_count"] == 0


def test_egress_blocked_is_logged_and_not_sent(tmp_path):
    engine, fake = make_engine(tmp_path)
    encoded = "".join(f"\\x{byte:02x}" for byte in b"password=Hunter22xy")
    state, bound = q.build_hints(f"dump {encoded}")
    decision = engine.decide("hints", state=state, questions=bound, items=[], legacy_action=["none"],
                             choose=lambda a: JevChoice(action=["decision"]))
    assert decision.fallback_reason == "egress_blocked" and decision.action == ["none"]
    assert fake.calls == []
    row = rows(tmp_path, "SELECT * FROM decisions")[0]
    assert row["state_redacted"] is None and row["transport_outcome"] == "not_sent"
    assert "Hunter22xy" not in json.dumps(row)


def test_budget_exhausted_is_not_sent(tmp_path):
    engine, fake = make_engine(tmp_path, env={"MEMORYMASTER_JEV_DAILY_USD_CAP": "0.01"})
    DecisionLedger(tmp_path / "decisions.db").write_decision(DecisionRecord(decision_id="spent", cost_usd=0.02))
    decision = decide_recall(engine)
    assert decision.fallback_reason == "budget_exhausted" and fake.calls == []


def test_breaker_open_behaves_as_shadow(tmp_path):
    """While open, a probe (the first request on the surface for 60 s) runs as shadow."""
    ledger = DecisionLedger(tmp_path / "decisions.db")
    for i in range(5):
        ledger.write_decision(DecisionRecord(decision_id=f"f{i}", surface="recall", attempt_count=1,
                                             transport_outcome="timeout",
                                             ts=utc_iso(datetime.now(timezone.utc) - timedelta(seconds=90 - i))))
    engine, fake = make_engine(tmp_path)
    decision = decide_recall(engine)
    assert decision.fallback_reason == "breaker_open" and decision.mode == "shadow"
    assert decision.action == LEGACY and len(fake.calls) == 1
    assert decision.jev_action == ["claim:1", "claim:2"]


@pytest.mark.parametrize("outcome", ["http_403", "http_429", "http_529", "malformed", "network_error", "too_large"])
def test_transport_failures_fall_back_with_reason(tmp_path, outcome):
    engine, fake = make_engine(tmp_path, transport=FakeTransport(outcome=outcome))
    decision = decide_recall(engine)
    assert decision.action == LEGACY and decision.fallback_reason == outcome
    row = rows(tmp_path, "SELECT transport_outcome, action_taken, jev_action FROM decisions")[0]
    assert row["transport_outcome"] == outcome and row["jev_action"] is None


def test_deadline_returns_legacy_and_late_answer_never_acts(tmp_path):
    slow = FakeTransport(delay=1.2)
    engine, _ = make_engine(tmp_path, transport=slow, env={"MEMORYMASTER_JEV_HOOK_DEADLINE_MS": "300"})
    engine.ledger.register_questions(q.get(key) for key in ("recall.relevant", "recall.usable_evidence",
                                                            "recall.contradicts_request", "recall.instruction_like"))
    started = time.perf_counter()  # the ledger exists: the hook deadline is spent waiting for the answer
    decision = decide_recall(engine)
    # deadline + 100 ms (the hook contract), plus one tick of the Windows monotonic clock the engine reads
    if TIMING:
        assert time.perf_counter() - started <= 0.3 + 0.1 + 0.016
    assert decision.action == LEGACY and decision.fallback_reason == "timeout"
    engine.wait_for_late_answers(timeout=5)
    row = rows(tmp_path, "SELECT * FROM decisions")[0]
    assert json.loads(row["action_taken"]) == LEGACY and row["transport_outcome"] == "timeout"
    late = rows(tmp_path, "SELECT * FROM outcomes WHERE kind = 'late_answer'")
    assert len(late) == 1 and late[0]["decision_id"] == decision.decision_id
    assert json.loads(late[0]["details_json"])["transport_outcome"] == "ok"
    answered = rows(tmp_path, "SELECT COUNT(*) AS n FROM decision_items WHERE question_id != ''")
    assert answered[0]["n"] == 12
    assert json.loads(rows(tmp_path, "SELECT action_taken FROM decisions")[0]["action_taken"]) == LEGACY


def test_answer_after_deadline_inside_transport_is_late(tmp_path):
    clock = {"t": 0.0}

    class Advancing(FakeTransport):
        def send(self, payload, **kwargs):
            clock["t"] += 5.0  # the answer arrives 5 s after the request started
            return super().send(payload, **kwargs)

    engine, _ = make_engine(tmp_path, transport=Advancing(), monotonic=lambda: clock["t"], env=PRODUCTION_HOOK)
    decision = decide_recall(engine)
    assert decision.fallback_reason == "late" and decision.action == LEGACY
    row = rows(tmp_path, "SELECT transport_outcome, jev_action, action_taken, latency_ms, engine_ms FROM decisions")[0]
    assert row["transport_outcome"] == "late" and row["jev_action"] is None
    assert json.loads(row["action_taken"]) == LEGACY
    # engine_ms is the whole decide() on the engine clock; latency_ms is what the transport reported
    assert row["engine_ms"] == 5000 and row["latency_ms"] == 42
    assert rows(tmp_path, "SELECT COUNT(*) AS n FROM decision_items WHERE question_id != ''")[0]["n"] == 12


def test_engine_ms_is_logged_next_to_transport_latency_on_every_row(tmp_path):
    engine, _ = make_engine(tmp_path, transport=FakeTransport(delay=0.06, latency_ms=1),
                            env={"MEMORYMASTER_JEV_EXPLORE_RECALL": "0"})
    assert decide_recall(engine).fallback_reason is None
    row = rows(tmp_path, "SELECT latency_ms, engine_ms FROM decisions")[0]
    assert row["latency_ms"] == 1
    assert row["engine_ms"] >= 50  # includes the transport wait and the engine's own work

    missing, fake = make_engine(tmp_path / "nokey", key=None)
    assert decide_recall(missing).fallback_reason == "missing_key" and fake.calls == []
    off, _ = make_engine(tmp_path / "off", mode="off", env={"MEMORYMASTER_DECISIONS_LOG_OFF": "1"})
    assert decide_recall(off).fallback_reason == "mode_off"
    for where in ("nokey", "off"):
        logged = rows(tmp_path / where, "SELECT latency_ms, engine_ms FROM decisions")[0]
        assert logged["latency_ms"] is None and isinstance(logged["engine_ms"], int) and logged["engine_ms"] >= 0


def test_choose_errors_and_unauthorized_ids_fall_back(tmp_path):
    engine, _ = make_engine(tmp_path)
    state, bound = recall_inputs()

    def boom(answers):
        raise RuntimeError("bad choose")

    d1 = engine.decide("recall", state=state, questions=bound, items=ITEMS, legacy_action=LEGACY, choose=boom)
    assert d1.fallback_reason == "choose_error" and d1.action == LEGACY
    d2 = engine.decide("recall", state=state, questions=bound, items=ITEMS, legacy_action=LEGACY,
                       choose=lambda a: JevChoice(order=["claim:1", "claim:999"], k=2))
    assert d2.fallback_reason == "choose_invalid" and d2.action == LEGACY


def test_invalid_request_never_raises(tmp_path):
    engine, fake = make_engine(tmp_path)
    decision = engine.decide("recall", state={"x": object()}, questions=[], items=[], legacy_action=LEGACY,
                             choose=lambda a: JevChoice(action=LEGACY))
    assert decision.action == LEGACY and decision.fallback_reason in {"invalid_request", "engine_error"}
    assert fake.calls == []


def _explored_id(surface: str, rate: float) -> str:
    for n in range(10_000):
        candidate = f"probe-{n}"
        if pl.exploration_u(candidate, surface) < rate:
            return candidate
    raise AssertionError("no exploring id found")


def _policy_id(surface: str, rate: float) -> str:
    for n in range(10_000):
        candidate = f"probe-{n}"
        if pl.exploration_u(candidate, surface) >= rate:
            return candidate
    raise AssertionError("no policy id found")


def test_ingest_exploration_admits_held_candidate_with_logged_propensity(tmp_path):
    state, bound = q.build_ingest("Use WAL", "we decided to use WAL", scope_label="project:mm", item_ref="candidate:1")

    def hold(answers):
        return JevChoice(action="hold", safe_alternative="admit", explore_arm="explore_admit")

    engine, _ = make_engine(tmp_path, ids=[_explored_id("ingest", 0.05), _policy_id("ingest", 0.05)])
    explored = engine.decide("ingest", state=state, questions=bound, items=[DecisionItem("candidate:1", "candidate")],
                             legacy_action="admit", choose=hold)
    kept = engine.decide("ingest", state=state, questions=bound, items=[DecisionItem("candidate:1", "candidate")],
                         legacy_action="admit", choose=hold)
    assert explored.action == "admit" and explored.exploration_arm == "explore_admit"
    assert kept.action == "hold" and kept.exploration_arm == "policy"
    by_id = {r["decision_id"]: r for r in rows(tmp_path, "SELECT * FROM decisions")}
    assert by_id[explored.decision_id]["chosen_propensity"] == pytest.approx(0.05)
    assert by_id[kept.decision_id]["chosen_propensity"] == pytest.approx(0.95)
    assert json.loads(by_id[explored.decision_id]["jev_action"]) == "hold"


def test_recall_exploration_reorders_top_five(tmp_path):
    engine, _ = make_engine(tmp_path, ids=[_explored_id("recall", 0.10)])
    decision = decide_recall(engine)
    assert decision.exploration_arm == "explore_order"
    row = rows(tmp_path, "SELECT * FROM decisions")[0]
    propensities = json.loads(row["action_propensities_json"])
    assert len(propensities) == 6 and math.isclose(sum(p["p"] for p in propensities), 1.0)
    ranks = rows(tmp_path, "SELECT DISTINCT item_ref, rank_final FROM decision_items "
                           "WHERE rank_final IS NOT NULL ORDER BY rank_final")
    ordering = [r["item_ref"] for r in ranks]
    match = [p["p"] for p in propensities if p["action"] == ordering]
    assert match == [pytest.approx(row["chosen_propensity"])]
    assert decision.action == ordering[:2]
    assert json.loads(row["action_taken"]) == decision.action
    assert json.loads(row["jev_action"]) == ["claim:1", "claim:2"]


def test_unwritable_ledger_never_raises(tmp_path):
    blocker = tmp_path / "decisions.db"
    blocker.mkdir()
    engine, fake = make_engine(tmp_path)
    decision = decide_recall(engine)
    assert decision.action == LEGACY
    assert decision.fallback_reason == "ledger_unavailable"
    assert fake.calls == []


class _JevServer:
    def __init__(self):
        self.bodies: list[dict] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):  # noqa: N802
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.bodies.append(payload)
                answers = {}
                for wire_id, question in payload["questions"].items():
                    if question["type"] == "noul":
                        answers[wire_id] = {"type": "noul", "noul": 0.7}
                    else:  # as the live API answers: keyed by 0-based index, legend echoes the criteria
                        last = len(question["criteria"]) - 1
                        answers[wire_id] = {"type": "score", "score": float(last), "confidence": 0.9,
                                            "legend": {str(i): c for i, c in enumerate(question["criteria"])},
                                            "probabilities": {str(i): (1.0 if i == last else 0.0)
                                                              for i in range(last + 1)}}
                body = json.dumps({"model": "jev-1.13.0", "answers": answers,
                                   "usage": {"input_tokens": 321, "output_tokens": 0}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/v1/systemone"


def test_end_to_end_with_real_transport_against_local_server(tmp_path):
    server = _JevServer()
    try:
        config = DecisionConfig.from_env({"MEMORYMASTER_JEV_MODE": "live",
                                          "MEMORYMASTER_DECISIONS_DB": str(tmp_path / "decisions.db"),
                                          "MEMORYMASTER_JEV_EXPLORE_RECALL": "0"})
        engine = DecisionEngine(config, key_lookup=lambda: KEY,
                                transport_factory=lambda key: HttpTransport(key, endpoint=server.url))
        decision = decide_recall(engine)
    finally:
        server.httpd.shutdown()
        server.httpd.server_close()
    assert decision.fallback_reason is None and decision.mode == "live"
    assert len(server.bodies) == 1 and "10.0.0.5" not in json.dumps(server.bodies[0])
    scores = [q for q in server.bodies[0]["questions"].values() if q["type"] == "score"]
    assert scores and all(isinstance(c, str) for q in scores for c in q["criteria"])
    row = rows(tmp_path, "SELECT * FROM decisions")[0]
    assert row["transport_outcome"] == "ok" and row["tokens_in"] == 321
    assert row["cost_usd"] == pytest.approx(321 * 0.042e-6)
    assert row["sdk_version"] == f"stdlib-http.client/py{sys.version_info[0]}.{sys.version_info[1]}"
    assert row["transport_version"] == "stdlib-inproc/2"


def test_module_level_decide_accepts_mapping_context(tmp_path):
    engine, fake = make_engine(tmp_path, env={"MEMORYMASTER_JEV_EXPLORE_RECALL": "0"})
    state, bound = recall_inputs()
    decision = eng.decide("recall", state=state, questions=iter(bound), items=ITEMS, legacy_action=LEGACY,
                          choose=recall_choose, context={"session_key": "s9", "kind": "batch"}, engine=engine)
    assert decision.action == ["claim:1", "claim:2"]
    assert fake.calls[0]["max_retries"] == 3 and fake.calls[0]["timeout_s"] == pytest.approx(8.0)
    assert rows(tmp_path, "SELECT session_key FROM decisions")[0]["session_key"] == "s9"


def test_default_engine_is_rebuilt_when_environment_changes(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMORYMASTER_DECISIONS_DB", str(tmp_path / "a.db"))
    first = eng.default_engine()
    assert eng.default_engine() is first
    monkeypatch.setenv("MEMORYMASTER_JEV_MODE", "shadow")
    assert eng.default_engine() is not first


def test_label_actions_are_not_mistaken_for_item_refs(tmp_path):
    engine, fake = make_engine(tmp_path)
    state, bound = q.build_hints("we decided to keep SQLite authoritative")

    def labels(answers):
        return JevChoice(action=[label for label in q.HINT_LABELS
                                 if (answers.noul(f"hints.{label}") or 0) >= answers.threshold(f"hints.{label}", "show")])

    decision = engine.decide("hints", state=state, questions=bound, items=[], legacy_action=["decision"],
                             choose=labels)
    assert decision.fallback_reason is None and decision.mode == "live"
    assert decision.action == list(q.HINT_LABELS)  # fake answers are 0.8 >= 0.7 for decision-level questions
    assert decision.items == []


def test_thresholds_are_registered_once_per_engine(tmp_path, monkeypatch):
    engine, _ = make_engine(tmp_path)
    calls = []
    original = engine.ledger.register_questions
    monkeypatch.setattr(engine.ledger, "register_questions", lambda specs: calls.append(1) or original(specs))
    decide_recall(engine)
    decide_recall(engine)
    assert calls == [1]


def test_dynamic_choice_options_keep_ids_and_redact_descriptions(tmp_path):
    engine, fake = make_engine(tmp_path)
    state, bound = q.build_skills("deploy the release", [
        ("skill:7", "skill:7", {"title": "Release", "when_to_use": "mail ops@corp.example first"}),
    ])
    decision = engine.decide("skills", state=state, questions=bound, items=[DecisionItem("skill:7", "skill")],
                             legacy_action=[], choose=lambda a: JevChoice(action=[a.choice("skills.which_skill")]))
    assert decision.fallback_reason is None
    criteria = fake.calls[0]["payload"]["questions"]["skills.which_skill"]["criteria"]
    assert set(criteria) == {"none", "skill:7"}
    assert "ops@corp.example" not in json.dumps(fake.calls[0]["payload"])
    assert decision.action == ["none"]  # fake answers pick the first option


def test_no_questions_or_duplicate_questions_are_invalid_requests(tmp_path):
    engine, fake = make_engine(tmp_path)
    empty = engine.decide("hints", state={"prompt": "x"}, questions=[], items=[], legacy_action=["none"],
                          choose=lambda a: JevChoice(action=["decision"]))
    spec = q.get("hints.decision")
    dup = engine.decide("hints", state={"prompt": "x"}, questions=[q.BoundQuestion(spec), q.BoundQuestion(spec)],
                        items=[], legacy_action=["none"], choose=lambda a: JevChoice(action=["decision"]))
    assert empty.fallback_reason == dup.fallback_reason == "invalid_request"
    assert empty.action == dup.action == ["none"] and fake.calls == []


def _scores_from_wrong_primitive(answers) -> JevChoice:
    """Verifier repro: a score looked up on a noul question is None."""
    choice = recall_choose(answers)
    choice.scores = {r: answers.score("recall.usable_evidence", r) for r in choice.order}
    return choice


def _scores_with(bad):
    def choose(answers) -> JevChoice:
        choice = recall_choose(answers)
        choice.scores["claim:2"] = bad
        return choice

    return choose


@pytest.mark.parametrize("choose", [_scores_from_wrong_primitive, _scores_with(None), _scores_with("high"),
                                    _scores_with(float("nan")), _scores_with(float("inf")), _scores_with(True)],
                         ids=["wrong_primitive", "none", "string", "nan", "inf", "bool"])
def test_invalid_scores_are_logged_and_budgeted(tmp_path, choose):
    """B1: bad scores used to crash exploration into an unlogged, unbudgeted engine_error."""
    engine, fake = make_engine(tmp_path)  # default config: recall explores at 0.10
    state, bound = recall_inputs()
    decisions = [engine.decide("recall", state=state, questions=bound, items=ITEMS, legacy_action=LEGACY,
                               choose=choose) for _ in range(3)]
    assert len(fake.calls) == 3
    assert [(d.action, d.fallback_reason, d.logged) for d in decisions] == [(LEGACY, "choose_invalid", True)] * 3
    logged = rows(tmp_path, "SELECT * FROM decisions ORDER BY decision_id")
    assert [r["decision_id"] for r in logged] == sorted(d.decision_id for d in decisions)
    for row in logged:
        assert row["transport_outcome"] == "ok" and row["attempt_count"] == 1 and row["tokens_in"] == 500
        assert json.loads(row["action_taken"]) == LEGACY
        propensities = json.loads(row["action_propensities_json"])
        assert all(math.isfinite(p["p"]) for p in propensities)
        assert math.isclose(sum(p["p"] for p in propensities), 1.0)
    since = utc_iso(datetime.now(timezone.utc) - timedelta(minutes=5))
    assert engine.ledger.spend_since(since) == pytest.approx(3 * 500 * 0.042e-6)
    assert engine.ledger.requests_since(since) == 3


def test_exception_after_transport_is_logged_with_spend(tmp_path, monkeypatch):
    """Any failure after the paid request still writes the row the budget and breaker read."""
    def explode(*args, **kwargs):
        raise OverflowError("math range error")

    monkeypatch.setattr(pl, "explore_ranking", explode)
    engine, fake = make_engine(tmp_path, env={"MEMORYMASTER_JEV_RPM_CAP": "2"})
    first, second = decide_recall(engine), decide_recall(engine)
    assert [(d.action, d.fallback_reason, d.logged) for d in (first, second)] == [(LEGACY, "engine_error", True)] * 2
    logged = rows(tmp_path, "SELECT * FROM decisions ORDER BY decision_id")
    assert len(logged) == 2
    for row in logged:
        assert row["fallback_reason"] == "engine_error" and row["transport_outcome"] == "ok"
        assert row["tokens_in"] == 500 and row["cost_usd"] == pytest.approx(500 * 0.042e-6)
        assert row["model_served"] == "jev-1.13.0" and json.loads(row["action_taken"]) == LEGACY
        assert row["exploration_arm"] == "fallback" and row["chosen_propensity"] == 1.0
    # Another hook process (fresh engine, full token bucket) sees both requests through the ledger RPM cap.
    other, other_fake = make_engine(tmp_path, env={"MEMORYMASTER_JEV_RPM_CAP": "2"}, ids=["other-0"])
    third = decide_recall(other)
    assert third.fallback_reason == "budget_exhausted" and other_fake.calls == [] and len(fake.calls) == 2


def test_malformed_answer_objects_after_transport_still_log_the_request(tmp_path):
    class Opaque:
        primitive = "score"
        value = 0.5  # no probabilities/confidence: item rows cannot be built from it

    class OddTransport(FakeTransport):
        def send(self, payload, *, expected, timeout_s, max_retries=0):
            result = super().send(payload, expected=expected, timeout_s=timeout_s, max_retries=max_retries)
            return TransportResult(200, result.body, 42, 1, "ok", model_served="jev-1.13.0", tokens_in=500,
                                   tokens_out=3, cost_usd=500 * 0.042e-6,
                                   answers={wire_id: Opaque() for wire_id in expected})

    engine, fake = make_engine(tmp_path, transport=OddTransport(), env={"MEMORYMASTER_JEV_EXPLORE_RECALL": "0"})
    decision = decide_recall(engine)
    assert decision.action == LEGACY and decision.fallback_reason == "engine_error" and decision.logged
    row = rows(tmp_path, "SELECT * FROM decisions")[0]
    assert row["transport_outcome"] == "ok" and row["tokens_in"] == 500 and row["cost_usd"] > 0
    exposed = rows(tmp_path, "SELECT item_ref FROM decision_items WHERE exposed = 1 ORDER BY item_ref")
    assert [r["item_ref"] for r in exposed] == ["claim:2", "claim:3"]


# ------------------------------------------------------ never act unlogged ---

@pytest.mark.parametrize("mode", ["live", "shadow"])
def test_an_answer_is_never_acted_on_when_its_decision_row_is_not_written(tmp_path, monkeypatch, mode):
    engine, fake = make_engine(tmp_path, mode=mode, env={"MEMORYMASTER_JEV_EXPLORE_RECALL": "0"})
    monkeypatch.setattr(engine.ledger, "write_decision", lambda record, items=(): False)
    decision = decide_recall(engine)
    assert len(fake.calls) == 1  # the answer arrived and Jev chose ["claim:1", "claim:2"]
    assert decision.action == LEGACY and decision.items == LEGACY
    assert decision.fallback_reason == "ledger_unavailable" and decision.logged is False
    assert decision.jev_action is None and decision.answers is None
    assert decision.exploration_arm == "fallback"


def test_an_unlogged_binary_decision_takes_the_legacy_action(tmp_path, monkeypatch):
    engine, _ = make_engine(tmp_path, ids=[_policy_id("ingest", 0.05)])
    monkeypatch.setattr(engine.ledger, "write_decision", lambda record, items=(): False)
    state, bound = q.build_ingest("Use WAL", "we decided to use WAL", scope_label="project:mm", item_ref="cand:1")
    decision = engine.decide("ingest", state=state, questions=bound, items=[DecisionItem("cand:1", "candidate")],
                             legacy_action="admit", choose=lambda a: JevChoice(action="hold"))
    assert (decision.action, decision.fallback_reason) == ("admit", "ledger_unavailable")


# ------------------------------------------------------------- hook bound ---

def _hold_write_lock(path) -> sqlite3.Connection:
    holder = sqlite3.connect(str(path), timeout=5, isolation_level=None, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")
    return holder


def test_hook_decide_under_a_write_locked_ledger_sends_nothing_and_is_bounded(tmp_path):
    """Question registration hits the lock: no request, legacy action, counted failures."""
    from memorymaster.decisions.ledger import ledger_write_failures

    path = tmp_path / "decisions.db"
    assert DecisionLedger(path).write_decision(DecisionRecord(decision_id="seed", surface="hints")) is True
    engine, fake = make_engine(tmp_path)  # a new hook process: nothing registered yet
    before = ledger_write_failures()
    holder = _hold_write_lock(path)
    try:
        started = time.perf_counter()
        decision = decide_recall(engine)
        elapsed = time.perf_counter() - started
    finally:
        holder.rollback()
        holder.close()
    if TIMING:
        assert elapsed < 1.2, elapsed
    assert fake.calls == []
    assert decision.action == LEGACY and decision.fallback_reason == "ledger_unavailable"
    assert ledger_write_failures() > before


def test_hook_busy_wait_is_configurable(tmp_path):
    path = tmp_path / "decisions.db"
    assert DecisionLedger(path).write_decision(DecisionRecord(decision_id="seed", surface="hints")) is True
    engine, fake = make_engine(tmp_path, env={"MEMORYMASTER_DECISIONS_HOOK_BUSY_MS": "40"})
    holder = _hold_write_lock(path)
    try:
        started = time.perf_counter()
        decision = decide_recall(engine)
        elapsed = time.perf_counter() - started
    finally:
        holder.rollback()
        holder.close()
    assert decision.fallback_reason == "ledger_unavailable" and fake.calls == []
    if TIMING:
        assert elapsed < 0.5, elapsed


def test_hook_decide_returns_within_deadline_plus_100ms_when_the_final_write_is_locked(tmp_path):
    """The transport hangs AND another writer takes the lock while it hangs: still bounded."""
    path = tmp_path / "decisions.db"
    holders: list = []

    class LockingHang(FakeTransport):
        def send(self, payload, **kwargs):
            holders.append(_hold_write_lock(path))
            return super().send(payload, **kwargs)

    # On a slow CI runner the work before the send alone can exceed 300 ms (the transport was
    # never reached): there the deadline is scaled so the locked final write is still exercised.
    deadline_ms, hang_s = (300, 1.5) if TIMING else (2000, 4.0)
    engine, fake = make_engine(tmp_path, transport=LockingHang(delay=hang_s),
                               env={"MEMORYMASTER_JEV_HOOK_DEADLINE_MS": str(deadline_ms)})
    try:
        started = time.perf_counter()
        decision = decide_recall(engine)
        elapsed = time.perf_counter() - started
    finally:
        for holder in holders:
            holder.rollback()
            holder.close()
    assert len(fake.calls) == 1 and decision.action == LEGACY
    assert decision.fallback_reason == "ledger_unavailable"  # the timeout row could not be written
    # deadline + 100 ms, plus one tick of the Windows monotonic clock the engine reads
    if TIMING:
        assert elapsed <= 0.3 + 0.1 + 0.016, elapsed
    engine.wait_for_late_answers(timeout=5)


def test_hook_transport_gets_only_the_time_left_before_the_deadline(tmp_path):
    clock = {"t": 0.0}
    engine, fake = make_engine(tmp_path, monotonic=lambda: clock["t"], env=PRODUCTION_HOOK)
    original = engine.ledger.register_questions

    def slow_registration(specs):
        clock["t"] += 0.4  # e.g. a writer held the lock for 400 ms before registration went through
        return original(specs)

    engine.ledger.register_questions = slow_registration
    decide_recall(engine)
    assert fake.calls[0]["timeout_s"] == pytest.approx(0.5)


# ---------------------------------------------------------- breaker probing ---

def _open_breaker(tmp_path, surface: str = "recall", *, last_request_s_ago: float = 30.0) -> None:
    """A tripped breaker (open for 15 more minutes) whose last request was ``last_request_s_ago``."""
    now = datetime.now(timezone.utc)
    ledger = DecisionLedger(tmp_path / "decisions.db")
    ledger.set_watermark(f"breaker:{surface}", utc_iso(now + timedelta(minutes=15)))
    ledger.write_decision(DecisionRecord(decision_id=f"last-{surface}", surface=surface, attempt_count=1,
                                         transport_outcome="timeout",
                                         ts=utc_iso(now - timedelta(seconds=last_request_s_ago))))


def test_open_breaker_with_a_recent_request_logs_without_sending(tmp_path):
    _open_breaker(tmp_path, last_request_s_ago=30)
    engine, fake = make_engine(tmp_path)
    started = time.perf_counter()
    decision = decide_recall(engine)
    if TIMING:
        assert time.perf_counter() - started < 0.5
    assert fake.calls == []
    assert decision.action == LEGACY and decision.fallback_reason == "breaker_open" and decision.logged
    row = rows(tmp_path, f"SELECT * FROM decisions WHERE decision_id = '{decision.decision_id}'")[0]
    assert row["fallback_reason"] == "breaker_open" and row["attempt_count"] == 0
    assert row["transport_outcome"] == "not_sent" and json.loads(row["action_taken"]) == LEGACY


def test_open_breaker_sends_one_shadow_probe_per_interval_across_processes(tmp_path):
    _open_breaker(tmp_path, last_request_s_ago=120)
    first, first_fake = make_engine(tmp_path, ids=["probe-a"])
    probe = decide_recall(first)
    assert len(first_fake.calls) == 1  # nothing sent on this surface for 60 s: one probe
    assert probe.fallback_reason == "breaker_open" and probe.mode == "shadow" and probe.action == LEGACY
    assert probe.jev_action == ["claim:1", "claim:2"]  # the probe's answer is logged, never acted on
    other, other_fake = make_engine(tmp_path, ids=["other-b"])  # another hook process
    assert decide_recall(other).fallback_reason == "breaker_open"
    assert other_fake.calls == [] and len(first_fake.calls) == 1
    assert decide_recall(first).fallback_reason == "breaker_open" and len(first_fake.calls) == 1



def test_breaker_rows_that_sent_nothing_are_fallback_arm_and_probes_are_shadow(tmp_path):
    """OPE must not mistake a no-request breaker row for a shadow observation of Jev."""
    _open_breaker(tmp_path, last_request_s_ago=120)
    engine, fake = make_engine(tmp_path, ids=["probe", "skipped"])
    decide_recall(engine)
    decide_recall(engine)
    arms = {r["decision_id"]: (r["exploration_arm"], r["attempt_count"]) for r in
            rows(tmp_path, "SELECT decision_id, exploration_arm, attempt_count FROM decisions "
                           "WHERE decision_id IN ('probe', 'skipped')")}
    assert len(fake.calls) == 1
    assert arms == {"probe": ("shadow", 1), "skipped": ("fallback", 0)}

def test_breaker_probe_interval_is_configurable(tmp_path):
    _open_breaker(tmp_path, last_request_s_ago=10)
    engine, fake = make_engine(tmp_path, env={"MEMORYMASTER_JEV_BREAKER_PROBE_S": "5"})
    assert decide_recall(engine).fallback_reason == "breaker_open" and len(fake.calls) == 1


def test_batch_surface_with_an_open_breaker_does_not_pay_the_deadline_per_item(tmp_path):
    """A TypeSafe hang used to cost the full batch deadline for every S3 candidate."""
    _open_breaker(tmp_path, "ingest", last_request_s_ago=120)
    hang = FakeTransport(delay=1.0)
    engine, _ = make_engine(tmp_path, transport=hang, env={"MEMORYMASTER_JEV_BATCH_DEADLINE_MS": "300"})
    state, bound = q.build_ingest("Use WAL", "we decided to use WAL", scope_label="project:mm", item_ref="cand:1")
    started = time.perf_counter()
    decisions = [engine.decide("ingest", state=state, questions=bound, items=[DecisionItem("cand:1", "candidate")],
                               legacy_action="admit", choose=lambda a: JevChoice(action="hold"),
                               context=DecisionContext(kind="batch")) for _ in range(6)]
    elapsed = time.perf_counter() - started
    engine.wait_for_late_answers(timeout=5)
    assert len(hang.calls) == 1  # one probe; the other five never touched the network
    assert [d.action for d in decisions] == ["admit"] * 6
    assert {d.fallback_reason for d in decisions} == {"breaker_open"}
    if TIMING:
        assert elapsed < 0.3 + 0.05 + 1.0, elapsed  # one deadline, not six
    logged = rows(tmp_path, "SELECT attempt_count FROM decisions WHERE surface = 'ingest' AND decision_id != 'last-ingest'")
    assert sorted(r["attempt_count"] for r in logged) == [0, 0, 0, 0, 0, 1]



def test_a_failed_breaker_read_sends_nothing_and_never_acts(tmp_path, monkeypatch):
    """Verifier B1: a watermark read that meets another writer's lock used to read as
    'no watermark', so an open breaker went live (acted on and paid for)."""
    _open_breaker(tmp_path, last_request_s_ago=5)
    engine, fake = make_engine(tmp_path)
    real_read = engine.ledger._read

    def locked_watermarks(sql, params=(), **kwargs):
        return None if "FROM watermarks" in sql else real_read(sql, params, **kwargs)

    monkeypatch.setattr(engine.ledger, "_read", locked_watermarks)
    decision = decide_recall(engine)
    assert fake.calls == []
    assert decision.action == LEGACY and decision.jev_action is None
    assert decision.fallback_reason == "ledger_unavailable"
    row = rows(tmp_path, f"SELECT * FROM decisions WHERE decision_id = '{decision.decision_id}'")[0]
    assert (row["attempt_count"], row["transport_outcome"]) == (0, "not_sent")



def test_a_failed_threshold_read_sends_nothing(tmp_path, monkeypatch):
    engine, fake = make_engine(tmp_path)
    real_read = engine.ledger._read

    def locked_thresholds(sql, params=(), **kwargs):
        return None if "FROM question_versions" in sql else real_read(sql, params, **kwargs)

    monkeypatch.setattr(engine.ledger, "_read", locked_thresholds)
    decision = decide_recall(engine)
    assert fake.calls == [] and decision.action == LEGACY
    assert decision.fallback_reason == "ledger_unavailable" and decision.logged

_BREAKER_HOOK_PROCESS = textwrap.dedent(
    """
    import json, sys
    from memorymaster.decisions import questions as q
    from memorymaster.decisions.config import DecisionConfig
    from memorymaster.decisions.credentials import ApiKey
    from memorymaster.decisions.engine import DecisionEngine, DecisionItem, JevChoice
    from memorymaster.decisions.transport import TransportResult
    path, rounds = sys.argv[1], int(sys.argv[2])
    sent = []

    class Counting:
        def send(self, payload, **kwargs):
            sent.append(1)
            return TransportResult(529, None, 1, 1, "http_529")

    config = DecisionConfig.from_env({"MEMORYMASTER_JEV_MODE": "live", "MEMORYMASTER_DECISIONS_DB": path})
    state, bound = q.build_recall("how do we run tests?", "memorymaster",
                                  [("claim:1", "Run pytest"), ("claim:2", "Use WAL")])
    reasons = []
    for _ in range(rounds):  # a new engine per call: a new hook process, nothing registered yet
        engine = DecisionEngine(config, transport_factory=lambda key: Counting(),
                                key_lookup=lambda: ApiKey("ts-test-" + "M" * 24))
        decision = engine.decide("recall", state=state, questions=bound,
                                 items=[DecisionItem("claim:1"), DecisionItem("claim:2")], legacy_action=["claim:1"],
                                 choose=lambda answers: JevChoice(action=["claim:2"]))
        reasons.append([decision.fallback_reason, decision.action == ["claim:1"]])
    print(json.dumps({"sent": len(sent), "reasons": reasons}))
    """
)


def test_open_breaker_holds_under_concurrent_hook_processes(tmp_path):
    """Verifier B1 regression: 12 hook processes against an open breaker whose last
    request is recent (no probe due) never send and never act."""
    _open_breaker(tmp_path, last_request_s_ago=1)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MEMORYMASTER_", "TYPESAFE_"))}
    env["PYTHONPATH"] = str(REPO)
    script = f"import sys; sys.path.insert(0, {str(REPO)!r})\n" + _BREAKER_HOOK_PROCESS
    procs = [subprocess.Popen([sys.executable, "-I", "-c", script, str(tmp_path / "decisions.db"), "8"],
                              cwd=str(tmp_path), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
             for _ in range(12)]
    outputs = [p.communicate(timeout=240) for p in procs]
    results = []
    for proc, (out, err) in zip(procs, outputs):
        assert proc.returncode == 0, err
        results.append(json.loads(out.strip().splitlines()[-1]))
    assert sum(r["sent"] for r in results) == 0, results
    reasons = [reason for r in results for reason in r["reasons"]]
    assert len(reasons) == 96 and all(legacy for _, legacy in reasons), reasons
    assert {reason for reason, _ in reasons} <= {"breaker_open", "ledger_unavailable"}, reasons
    sent_rows = rows(tmp_path, "SELECT COUNT(*) AS n FROM decisions WHERE attempt_count > 0 "
                               "AND decision_id != 'last-recall'")
    assert sent_rows[0]["n"] == 0


# ------------------------------------------------ public logging (skip rows) ---

def test_record_skip_logs_a_not_sent_row_with_the_current_mode(tmp_path):
    engine, fake = make_engine(tmp_path, mode="shadow")
    logged = engine.record_skip("recall", reason="short_prompt", legacy_action=LEGACY, session_key="s-7",
                                state={"request": "ping 10.0.0.5"}, items=ITEMS)
    assert logged is True and fake.calls == []
    row = rows(tmp_path, "SELECT * FROM decisions")[0]
    assert (row["surface"], row["mode"], row["fallback_reason"]) == ("recall", "shadow", "skip:short_prompt")
    assert (row["attempt_count"], row["transport_outcome"], row["cost_usd"]) == (0, "not_sent", 0.0)
    assert json.loads(row["legacy_action"]) == json.loads(row["action_taken"]) == LEGACY
    assert row["session_key"] == "s-7" and row["exploration_arm"] == "fallback" and row["chosen_propensity"] == 1.0
    assert "10.0.0.5" not in row["state_redacted"] and "[REDACTED:private_ip]" in row["state_redacted"]
    assert json.loads(row["redaction_counts_json"]) == {"private_ip": 1}
    exposed = rows(tmp_path, "SELECT item_ref FROM decision_items WHERE exposed = 1 ORDER BY item_ref")
    assert [r["item_ref"] for r in exposed] == ["claim:2", "claim:3"]
    assert rows(tmp_path, "SELECT COUNT(*) AS n FROM decision_items")[0]["n"] == 3


def test_record_skip_follows_off_mode_like_the_engine(tmp_path):
    engine, _ = make_engine(tmp_path, mode="off")
    assert engine.record_skip("hints", reason="short_prompt", legacy_action=[]) is False
    assert not (tmp_path / "decisions.db").exists()  # off stays byte-identical: no ledger file
    logged_off, _ = make_engine(tmp_path, mode="off", env={"MEMORYMASTER_DECISIONS_LOG_OFF": "1"})
    assert logged_off.record_skip("hints", reason="short_prompt", legacy_action=[]) is True
    assert rows(tmp_path, "SELECT mode, fallback_reason FROM decisions") == [
        {"mode": "off", "fallback_reason": "skip:short_prompt"}]


def test_record_skip_never_raises_and_never_stores_an_unredactable_state(tmp_path):
    blocker = tmp_path / "blocked" / "decisions.db"
    blocker.mkdir(parents=True)
    broken, _ = make_engine(tmp_path / "blocked")
    assert broken.record_skip("recall", reason="x", legacy_action=[object()], items=["claim:1"]) is False
    engine, _ = make_engine(tmp_path)
    encoded = "".join(f"\\x{byte:02x}" for byte in b"password=Hunter22xy")
    assert engine.record_skip("recall", reason="query_sensitive", legacy_action=[],
                              state={"request": f"dump {encoded}"}) is True
    row = rows(tmp_path, "SELECT * FROM decisions")[0]
    assert row["state_redacted"] is None and "Hunter22xy" not in json.dumps(row)
    assert engine.record_skip(None, reason=None, legacy_action=None) is False  # nonsense in, no row, no raise


def test_record_skip_is_bounded_on_a_write_locked_ledger(tmp_path):
    path = tmp_path / "decisions.db"
    assert DecisionLedger(path).write_decision(DecisionRecord(decision_id="seed", surface="recall")) is True
    engine, _ = make_engine(tmp_path)
    holder = _hold_write_lock(path)
    try:
        started = time.perf_counter()
        logged = engine.record_skip("recall", reason="short_prompt", legacy_action=LEGACY)
        elapsed = time.perf_counter() - started
    finally:
        holder.rollback()
        holder.close()
    assert logged is False
    if TIMING:
        assert elapsed < 0.6, elapsed


def test_fallback_record_is_the_public_not_sent_row():
    record = eng.fallback_record("d-1", "skills", mode="live", reason="catalog_private", legacy_action=["claim:4"],
                                 baseline_features={"withheld": "catalog_private"}, session_key="s", scope="p",
                                 tenant="t", engine_ms=7)
    assert (record.decision_id, record.surface, record.mode, record.fallback_reason) == (
        "d-1", "skills", "live", "catalog_private")
    assert (record.transport_outcome, record.attempt_count, record.tokens_in, record.cost_usd) == ("not_sent", 0, 0, 0.0)
    assert json.loads(record.action_taken) == json.loads(record.legacy_action) == ["claim:4"]
    assert json.loads(record.action_propensities_json) == [{"action": ["claim:4"], "p": 1.0}]
    assert record.randomization_id == pl.randomization_id("d-1", "skills") and record.engine_ms == 7
    assert record.code_revision and record.policy_version == pl.POLICY_VERSION
    items = eng.fallback_item_rows("d-1", ["claim:4", "claim:5"], kind="skill", delivered=["claim:5"])
    assert [(i.item_ref, i.item_kind, i.rank_legacy, i.rank_final, i.exposed, i.delivered) for i in items] == [
        ("claim:4", "skill", 1, 1, 1, 0), ("claim:5", "skill", 2, 2, 1, 1)]


def test_skill_withheld_rows_use_only_public_engine_helpers():
    import ast

    source = (REPO / "memorymaster" / "knowledge" / "jev_selector.py").read_text(encoding="utf-8")
    private = [alias.name for node in ast.walk(ast.parse(source))
               if isinstance(node, ast.ImportFrom) and node.module == "memorymaster.decisions.engine"
               for alias in node.names if alias.name.startswith("_")]
    assert private == []


# ------------------------------------------- wave 3 verifier round 2 (non-blocking) ---

class _Utf8Transport(FakeTransport):
    """Serializes the body like HttpTransport: text that is not valid UTF-8 is request_invalid."""

    def send(self, payload, *, expected, timeout_s, max_retries=0):
        try:
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            self.calls.append({"payload": None})
            return TransportResult(None, None, 0, 0, "request_invalid", error_class=type(exc).__name__)
        return super().send(payload, expected=expected, timeout_s=timeout_s, max_retries=max_retries)


def test_a_lone_surrogate_in_the_request_is_scrubbed_sent_and_logged_as_sent(tmp_path):
    """A "\\ud800" JSON escape in a prompt is replaced by U+FFFD before redaction: the
    request is valid UTF-8, it is sent, and the ledger holds exactly the text that left."""
    engine, fake = make_engine(tmp_path, transport=_Utf8Transport())
    prompt = json.loads('"run the tests \\ud800 now"')
    state, bound = q.build_recall(prompt, "memorymaster", [("claim:1", "Run pytest")])
    decision = engine.decide("recall", state=state, questions=bound, items=["claim:1"], legacy_action=["claim:1"],
                             choose=recall_choose)
    assert decision.logged is True and decision.fallback_reason is None, decision
    assert fake.calls and fake.calls[0]["payload"] is not None
    stored = rows(tmp_path, "SELECT state_redacted FROM decisions")
    logged = json.loads(stored[0]["state_redacted"])["state"]["request"]
    assert logged == "run the tests \ufffd now" == fake.calls[0]["payload"]["state"]["request"]


def _slow_egress(monkeypatch, per_call_s: float) -> list:
    from memorymaster.decisions import egress

    real = egress.prepare_egress_value
    calls: list = []

    def slow(value, **kwargs):
        calls.append(value)
        time.sleep(per_call_s)
        return real(value, **kwargs)

    monkeypatch.setattr(egress, "prepare_egress_value", slow)
    return calls


def test_each_distinct_subject_is_redacted_once(tmp_path, monkeypatch):
    """A recall candidate is the subject of four questions: one redaction, not four."""
    calls = _slow_egress(monkeypatch, 0.0)
    engine, fake = make_engine(tmp_path)
    decision = decide_recall(engine)
    assert decision.fallback_reason is None and len(fake.calls) == 1
    assert len(calls) == 1 + 3, [type(value).__name__ for value in calls]  # the state + three candidates


def test_hook_egress_stops_at_the_deadline(tmp_path, monkeypatch):
    """Egress is CPU work before the send: a hook decision still returns by deadline + 100 ms."""
    per_call = 0.05
    calls = _slow_egress(monkeypatch, per_call)
    engine, fake = make_engine(tmp_path, env={"MEMORYMASTER_JEV_HOOK_DEADLINE_MS": "300"})
    candidates = [(f"claim:{n}", f"candidate memory number {n} about the build") for n in range(20)]
    state, bound = q.build_recall("how do we run tests?", "memorymaster", candidates)
    refs = [ref for ref, _ in candidates]
    started = time.perf_counter()
    decision = engine.decide("recall", state=state, questions=bound, items=refs, legacy_action=refs[:5],
                             choose=recall_choose)
    elapsed = time.perf_counter() - started
    assert fake.calls == [] and decision.action == refs[:5]
    assert decision.fallback_reason == "timeout" and decision.logged is True
    # deadline + 100 ms, plus at most the one egress call already running, plus one clock tick
    if TIMING:
        assert elapsed <= 0.3 + 0.1 + per_call + 0.016, (elapsed, len(calls))
    stored = rows(tmp_path, "SELECT fallback_reason, attempt_count, transport_outcome FROM decisions")
    assert stored == [{"fallback_reason": "timeout", "attempt_count": 0, "transport_outcome": "not_sent"}]


@pytest.mark.parametrize("warm", [False, True], ids=["fresh-engine", "warmed-engine"])
def test_registered_questions_and_a_write_lock_still_send_nothing(tmp_path, warm):
    """Wave-3 verifier B1: once question versions are stored, every pre-send step is a
    read, so without a write before the send a locked ledger let hooks send (and pay)
    with no row. A durable send intent must be written first; if it cannot, nothing is sent."""
    path = tmp_path / "decisions.db"
    first, first_fake = make_engine(tmp_path)
    assert decide_recall(first).logged is True  # stores the recall question versions
    engine, fake = (first, first_fake) if warm else make_engine(tmp_path, ids=[f"late-{n}" for n in range(10)])
    sent_before = len(fake.calls)
    holder = _hold_write_lock(path)
    try:
        started = time.perf_counter()
        decision = decide_recall(engine)
        elapsed = time.perf_counter() - started
    finally:
        holder.rollback()
        holder.close()
    assert len(fake.calls) == sent_before, "a request left while the ledger could not record it"
    assert decision.action == LEGACY and decision.fallback_reason == "ledger_unavailable"
    if TIMING:
        assert elapsed < 1.2, elapsed


def test_every_sent_request_leaves_a_durable_intent_counted_by_the_budget(tmp_path):
    """If the final write fails after the send, the request's cost still reaches the
    daily cap and the RPM cap through its send intent (no silent spend)."""
    from memorymaster.decisions.policy import Budget

    path = tmp_path / "decisions.db"
    holders: list = []

    class LockAfterSend(FakeTransport):
        def send(self, payload, **kwargs):
            result = super().send(payload, **kwargs)
            if len(self.calls) == 2:  # the second decision's final write will fail
                holders.append(_hold_write_lock(path))
            return result

    engine, fake = make_engine(tmp_path, transport=LockAfterSend())
    decide_recall(engine)  # warm: registers questions, one logged decision
    try:
        decision = decide_recall(engine)
    finally:
        for holder in holders:
            holder.rollback()
            holder.close()
    assert len(fake.calls) == 2 and decision.logged is False and decision.action == LEGACY
    ledger = DecisionLedger(path)
    intents = ledger.query("SELECT decision_id, est_cost_usd FROM send_intents ORDER BY ts")
    assert [row["decision_id"] for row in intents] == ["dec-0", "dec-1"]
    orphan = ledger.query("SELECT i.decision_id FROM send_intents i LEFT JOIN decisions d USING (decision_id) "
                          "WHERE d.decision_id IS NULL")
    assert [row["decision_id"] for row in orphan] == ["dec-1"]
    config = DecisionConfig.from_env({"MEMORYMASTER_JEV_MODE": "live", "MEMORYMASTER_DECISIONS_DB": str(path),
                                      "MEMORYMASTER_JEV_DAILY_USD_CAP": "1e-12"})
    assert intents[1]["est_cost_usd"] > 0
    assert Budget(ledger, config).check() == "budget_exhausted"
    assert ledger.requests_since("1970-01-01T00:00:00+00:00") == 2


def test_a_lone_surrogate_is_scrubbed_before_egress_so_what_is_redacted_is_what_is_sent(tmp_path):
    """Review of 8c8d57e: the transport turned a lone surrogate into '?' AFTER redaction,
    so '<surrogate>phone=...' left as '?phone=...' unredacted and the ledger logged a
    different text. Surrogates are replaced before redaction; the wire and the ledger agree."""
    engine, fake = make_engine(tmp_path)
    query = "ping https://api.whatsapp.com/send\ud83dphone=5491122334455 and https://h.example.org/in\ud83dsig=abcDEF123ghi456"
    state, bound = q.build_recall(query, "memorymaster", [("claim:3", "old note"), ("claim:2", "Use WAL")])
    engine.decide("recall", state=state, questions=bound, items=ITEMS, legacy_action=LEGACY, choose=recall_choose,
                  context=DecisionContext(session_key="sess-1", legacy_exposed=LEGACY))
    assert len(fake.calls) == 1
    wire = json.dumps(fake.calls[0]["payload"], ensure_ascii=False)
    assert not any(0xD800 <= ord(ch) <= 0xDFFF for ch in wire)
    assert "?phone=" not in wire and "?sig=" not in wire
    logged = rows(tmp_path, "SELECT state_redacted FROM decisions")[0]["state_redacted"]
    assert json.loads(logged)["state"] == fake.calls[0]["payload"]["state"]


def test_waiting_for_the_send_intent_is_charged_to_the_hook_deadline(tmp_path):
    """Review of f4096fb: reserve_send could wait for another writer and the request still
    got the full deadline, so a hook acted live on an answer that came after its deadline."""
    import threading

    path = tmp_path / "decisions.db"
    engine, fake = make_engine(tmp_path, transport=FakeTransport(delay=0.33),
                               env={"MEMORYMASTER_JEV_HOOK_DEADLINE_MS": "400"})
    decide_recall(engine)  # warm: questions registered, nothing else writes before the intent
    holder = _hold_write_lock(path)
    threading.Timer(0.22, lambda: (holder.rollback(), holder.close())).start()
    started = time.perf_counter()
    decision = decide_recall(engine)
    elapsed = time.perf_counter() - started
    assert decision.fallback_reason is not None and decision.action == LEGACY, decision
    if TIMING:
        assert elapsed < 0.4 + 0.1 + 0.05, elapsed

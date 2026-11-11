"""Egress gate on the remaining provider call sites: consolidation (worker + backfill), observation
synthesis, contradiction probe, entity graph, atlas extractor and digest. Synthetic fixtures; every stub
records the exact text it was handed."""

from __future__ import annotations

import json
import re

import pytest
from dataclasses import replace
from pathlib import Path


from memorymaster.dreaming.models import ConsolidationResult, DreamCandidate, DreamDecision, ProviderUsage
from test_dreaming_worker import NOW, _capture, _service
from test_provider_egress_gate import TOKEN, BENIGN, BLOCKED, IP, MAIL, WIN_PATH, B64, _assert_clean

ROOT = Path(__file__).resolve().parents[1]
USAGE = ProviderUsage("stub", "stub", 200, 1, 10, 2, True)


def _candidate(cid: str, text: str, quote: str = "Blue interfaces") -> DreamCandidate:
    return DreamCandidate(cid, text, "decision", "interface", "uses", "blue", "project", "m2", quote, 0.8)


class _Recorder:
    provider, model = "stub", "stub-consolidator"

    def __init__(self) -> None:
        self.calls: list[tuple[list, list]] = []

    def consolidate(self, candidates, current_claims, *, scope):
        self.calls.append((list(candidates), list(current_claims)))
        return ConsolidationResult(
            tuple(DreamDecision(c.candidate_id, "ignore", "stub", 0.9) for c in candidates), USAGE
        )


def _dump(calls) -> str:
    return json.dumps([[[c.to_dict() for c in cs], rs] for cs, rs in calls], ensure_ascii=False, default=str)


# ---- consolidation: worker ------------------------------------------------------------------


def test_worker_consolidation_gates_candidates_and_their_source_context(tmp_path):
    from memorymaster.dreaming.ledger import DreamLedger
    from memorymaster.dreaming.worker import DreamConfig, DreamWorker

    service = _service(tmp_path)
    ledger = DreamLedger(tmp_path / "capture.db")
    needles = f" host {IP} file {WIN_PATH} mail {MAIL} cfg {B64}"
    capture_id = _capture(ledger, extra_text=needles)
    candidates = [
        _candidate("leaky", f"The lab host {IP} is run by {MAIL} from {WIN_PATH}."),
        _candidate("blocked", "The deploy uses " + BLOCKED + " for the database."),
        _candidate("benign", BENIGN),
    ]
    ledger.set_extraction(capture_id, [c.to_dict() for c in candidates], "fixture")

    class NoExtract:
        model, provider = "unused", "unused"

        def __getattr__(self, name):
            raise AssertionError(name)

    consolidator = _Recorder()
    summary = DreamWorker(ledger, service, NoExtract(), consolidator, config=DreamConfig(), now=lambda: NOW).run(
        apply_candidates=False
    )
    assert summary["errors"] == 0
    assert summary["egress_withheld_consolidation"] >= 1
    sent = [c for cs, _ in consolidator.calls for c in cs]
    assert {c.candidate_id for c in sent} == {"leaky", "benign"}  # the blocked one is never sent
    dump = _dump(consolidator.calls)
    _assert_clean(dump)
    assert "hunter2" not in dump
    decisions = ledger.get_capture(capture_id)["decisions"]
    assert {d["candidate_id"]: d["action"] for d in decisions} == {
        "leaky": "ignore",
        "blocked": "ignore",
        "benign": "ignore",
    }


def test_gate_consolidation_drops_blocked_references_and_redacts_the_rest():
    from memorymaster.dreaming.consolidation_egress import gate_consolidation

    references = [
        {"id": 1, "text": f"The host is {IP} and the owner is {MAIL}", "subject": "host"},
        {"id": 2, "text": "The deploy uses " + BLOCKED, "subject": "deploy"},
        {"id": 3, "text": BENIGN, "subject": "retry", "object_value": None},
    ]
    sent, ignored, kept, withheld = gate_consolidation([_candidate("c", BENIGN)], references)
    assert [c.candidate_id for c in sent] == ["c"] and ignored == []
    assert [r["id"] for r in kept] == [1, 3] and withheld == 1
    _assert_clean(json.dumps(kept))


# ---- consolidation + all-withheld extraction: backfill --------------------------------------


def _backfill_fixture(tmp_path, monkeypatch, text: str):
    import test_dreaming_backfill_cli as bf

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("MEMORYMASTER_SCOPE_ALIASES", str(tmp_path / "fixture-aliases.json"))
    monkeypatch.setattr(bf.MemoryService, "_init_qdrant", staticmethod(lambda: None))
    monkeypatch.setattr("memorymaster.core.webhook.fire_webhook", lambda *args, **kwargs: None)
    monkeypatch.setattr("memorymaster.knowledge.entity_ingest.resolve_claim_entities", lambda *args, **kwargs: 0)
    bf.reset_intake_state()
    fixture = bf._campaign(tmp_path)
    source = fixture.sources / "session-0.jsonl"
    lines = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines()]
    lines[-1]["payload"]["content"][0]["text"] = text
    source.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return bf, fixture


def test_backfill_window_of_only_withheld_messages_makes_no_provider_call(tmp_path, monkeypatch):
    _bf, fixture = _backfill_fixture(tmp_path, monkeypatch, "We decided the deploy config is " + BLOCKED + " for good.")
    fixture.runner.inventory()
    fixture.runner.run(stage="extract")
    assert fixture.extractor.calls == []
    windows = [w for row in fixture.runner._rows() for w in row["metadata"]["selection"]["windows"]]
    assert windows and all(w.get("capture_id") for w in windows)  # the window completed without a call


def test_backfill_consolidation_gates_candidates(tmp_path, monkeypatch):
    _bf, fixture = _backfill_fixture(
        tmp_path, monkeypatch, "We decided the project must keep cited architecture decisions."
    )
    sensitive = [f"The lab host {IP} is run by {MAIL}.", "The deploy uses " + BLOCKED + " for the database."]
    real = fixture.extractor.extract

    def extract(messages, *, scope, capture_hash):
        base = real(messages, scope=scope, capture_hash=capture_hash)
        extra = tuple(
            replace(base.candidates[0], candidate_id=f"{capture_hash[:12]}-x{i}", text=text, object_value=f"object-{i}")
            for i, text in enumerate(sensitive)
        )
        return type(base)((*base.candidates[:1], *extra), base.usage)

    recorder = _Recorder()
    fixture.extractor.extract = extract
    fixture.runner.consolidator = recorder
    fixture.runner.inventory()
    fixture.runner.run(stage="extract")
    fixture.runner.run(stage="consolidate")
    sent = [c for cs, _ in recorder.calls for c in cs]
    assert sent and all("hunter2" not in c.text for c in sent) and len(sent) == 2
    dump = _dump(recorder.calls)
    assert IP not in dump and MAIL not in dump and "hunter2" not in dump
    assert all(row["status"] == "consolidated" for row in fixture.runner._rows())  # the withheld one was ignored


# ---- observation synthesis ------------------------------------------------------------------


def test_observation_prompt_is_gated_and_a_blocked_component_completes_as_no_signal(tmp_path, monkeypatch):
    from memorymaster.dreaming.ledger import DreamLedger
    from memorymaster.dreaming.worker import DreamConfig, DreamWorker

    sent: list[str] = []
    monkeypatch.setattr("memorymaster.core.llm_provider.call_llm", lambda system, prompt: sent.append(prompt) or "{}")
    worker = DreamWorker(DreamLedger(tmp_path / "c.db"), _service(tmp_path), None, _Recorder(), config=DreamConfig())
    answer = worker._observation_llm("sys", json.dumps({"claims": [{"text": f"host {IP} owner {MAIL} at {WIN_PATH}"}]}))
    assert answer == "{}" and len(sent) == 1
    _assert_clean(sent[0])
    blocked = worker._observation_llm("sys", json.dumps({"claims": [{"text": BLOCKED}, {"text": BENIGN}]}))
    assert json.loads(blocked) == {"decision": "no_signal"} and len(sent) == 1
    assert worker.observations_withheld == 1


# ---- contradiction probe --------------------------------------------------------------------


def _claim(text: str):
    from types import SimpleNamespace

    return SimpleNamespace(text=text)


def test_contradiction_probe_gates_both_claims(monkeypatch):
    from memorymaster.govern import contradiction_probe as probe

    seen: list[str] = []
    monkeypatch.setattr(probe.llm_provider, "call_llm", lambda prompt, body: seen.append(body) or "[]")
    probe._judge_llm(
        _claim(f"The primary lives at {IP} owned by {MAIL}"),
        _claim(f"The replica lives at {IP} owned by {MAIL}"),
    )
    assert len(seen) == 1
    _assert_clean(seen[0])
    # the same value gets the same pseudonym in both claims
    assert seen[0].count("[REDACTED:private_ipv4#1]") == 2 and seen[0].count("[REDACTED:email#2]") == 2


def test_contradiction_probe_pseudonyms_keep_swapped_values_distinguishable(monkeypatch):
    from memorymaster.govern import contradiction_probe as probe

    seen: list[str] = []
    monkeypatch.setattr(probe.llm_provider, "call_llm", lambda prompt, body: seen.append(body) or "[]")
    probe._judge_llm(_claim("The primary lives at 10.1.2.3"), _claim("The primary lives at 10.9.8.7"))
    assert seen == [
        "Claim A: The primary lives at [REDACTED:private_ipv4#1]\nClaim B: The primary lives at [REDACTED:private_ipv4#2]"
    ]


def test_contradiction_probe_skips_a_blocked_pair_and_a_pair_with_a_literal_credential(monkeypatch):
    from memorymaster.govern import contradiction_probe as probe

    seen: list[str] = []
    monkeypatch.setattr(probe.llm_provider, "call_llm", lambda prompt, body: seen.append(body) or "[]")
    assert probe._judge_llm(_claim("deploy " + BLOCKED), _claim(BENIGN)) is None
    assert probe._judge_llm(_claim("the key is " + TOKEN), _claim("the key is another")) is None
    assert seen == []


# ---- entity graph ---------------------------------------------------------------------------


def test_entity_graph_gates_claim_text_and_a_blocked_claim_yields_no_entities(monkeypatch):
    from memorymaster.knowledge import entity_graph as graph

    seen: list[str] = []
    monkeypatch.setattr(
        graph, "_llm_chat", lambda prompt, system="": seen.append(prompt) or '{"entities": [], "relations": []}'
    )
    obj = graph.EntityGraph.__new__(graph.EntityGraph)
    obj.last_diagnostics = []
    obj.ontology = type("O", (), {"prompt": staticmethod(lambda: "ontology")})()
    obj._get_known_entity_names = lambda limit, conn: []
    obj._payload_values_valid = lambda data: True
    obj._extract_payload(None, f"The host {IP} is run by {MAIL} from {WIN_PATH}")
    assert len(seen) == 1
    _assert_clean(seen[0])
    assert obj._extract_payload(None, "deploy " + BLOCKED) is None
    assert len(seen) == 1 and "egress_withheld" in obj.last_diagnostics


# ---- atlas extractor and digest -------------------------------------------------------------


def test_atlas_extractor_gates_raw_bodies_line_by_line(monkeypatch):
    from memorymaster.bridges import atlas_llm_extractor as atlas

    seen: list[str] = []
    monkeypatch.setattr(atlas, "call_llm", lambda prompt, text: seen.append(text) or "[]")
    body = "\n".join([f"mail from {MAIL} about {IP} in {WIN_PATH}", "pass " + BLOCKED, BENIGN])
    atlas._call_llm_safe("PROMPT", body, model=None)
    assert len(seen) == 1
    _assert_clean(seen[0])
    assert "hunter2" not in seen[0] and BENIGN in seen[0]
    assert atlas._call_llm_safe("PROMPT", BLOCKED, model=None) is None and len(seen) == 1


def test_atlas_digest_prompt_leaves_out_blocked_lines_and_redacts_the_rest():
    import importlib.util

    spec = importlib.util.spec_from_file_location("atlas_weekly_digest_t", ROOT / "scripts" / "atlas_weekly_digest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rows = [
        ("event", "Casa", f"Visita de {MAIL} en {WIN_PATH} el 12/12", None),
        ("event", "Banco", "clave " + BLOCKED, None),
        ("commitment", "Pago", BENIGN, "2026-12-01"),
    ]
    prompt = module.build_prompt(rows, "10/11/2026")
    _assert_clean(prompt)
    assert "hunter2" not in prompt and BENIGN in prompt


# ---- no new ungated call_llm site -----------------------------------------------------------

GATED = {
    "memorymaster/bridges/atlas_llm_extractor.py",
    "memorymaster/config_templates/hooks/memorymaster-auto-ingest.py",
    "memorymaster/dreaming/worker.py",
    "memorymaster/govern/contradiction_probe.py",
    "memorymaster/knowledge/entity_graph.py",
    "memorymaster/surfaces/session_end_ingest.py",
    "scripts/agent_session_end_ingest.py",
}
DORMANT = {  # ungated, dormant: gate before enabling (see core/provider_egress.py)
    "memorymaster/core/llm_budget.py",
    "memorymaster/core/llm_provider.py",
    "memorymaster/govern/auto_resolver.py",
    "memorymaster/govern/jobs/compact_summaries.py",
    "memorymaster/govern/llm_steward.py",
    "memorymaster/knowledge/entity_extractor.py",
    "memorymaster/knowledge/rule_miner.py",
    "memorymaster/knowledge/skills.py",
    "memorymaster/knowledge/vault_curator.py",
    "memorymaster/knowledge/vault_linter.py",
    "memorymaster/knowledge/wiki_engine.py",
    "memorymaster/recall/llm_rerank.py",
    "memorymaster/recall/selfcheck.py",
    "memorymaster/workflow_intelligence/classification.py",
    "scripts/label_prompts_with_judge.py",
}
_CALL = re.compile(r"(?<![\w.])(?:\w+\.)?_?call_llm\(")


def test_no_new_ungated_call_llm_call_site():
    found = set()
    for base in ("memorymaster", "scripts"):
        for path in (ROOT / base).rglob("*.py"):
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if _CALL.search(line) and not line.lstrip().startswith(("def ", "#")):
                    found.add(path.relative_to(ROOT).as_posix())
    unknown = found - GATED - DORMANT
    assert not unknown, (
        f"new call_llm call site(s) {sorted(unknown)}: gate them with core.provider_egress, then list them in GATED"
    )
    for rel in GATED:
        assert "egress import" in (ROOT / rel).read_text(encoding="utf-8"), rel + " lost its egress gate"


# ---- consolidation: trusted candidate markers, gate faults, reward ledger -------------------


def test_a_candidate_quoting_gated_text_with_a_glued_marker_is_not_withheld_again():
    from memorymaster.core.provider_egress import export_text
    from memorymaster.dreaming.consolidation_egress import gate_consolidation

    first_pass = export_text("The config at /home/jdoe/proj/app.yml was updated")
    assert first_pass is not None and "[REDACTED:" in first_pass and "jdoe" not in first_pass
    sent, ignored, _refs, _n = gate_consolidation([_candidate("c", first_pass)], [])
    assert [c.candidate_id for c in sent] == ["c"] and ignored == []


def test_a_gate_fault_is_not_a_verdict_the_capture_is_retried(tmp_path, monkeypatch):
    import memorymaster.core.provider_egress as gate
    from memorymaster.dreaming.ledger import DreamLedger
    from memorymaster.dreaming.worker import DreamConfig, DreamWorker

    real = gate._gate

    def flaky(text, **kwargs):
        if "poison" in text:
            raise ValueError("synthetic gate fault")
        return real(text, **kwargs)

    monkeypatch.setattr(gate, "_gate", flaky)
    service = _service(tmp_path)
    ledger = DreamLedger(tmp_path / "capture.db")
    capture_id = _capture(ledger)
    ledger.set_extraction(capture_id, [_candidate("p", "poison pill fact").to_dict()], "fixture")
    consolidator = _Recorder()
    summary = DreamWorker(ledger, service, None, consolidator, config=DreamConfig(), now=lambda: NOW).run(
        apply_candidates=False
    )
    assert summary["errors"] >= 1 and consolidator.calls == []
    row = ledger.get_capture(capture_id)
    assert row["state"] == "retryable" and not row.get("decisions")


def test_a_withheld_candidate_is_not_written_to_the_reward_ledger_as_a_model_rejection(tmp_path, monkeypatch):
    from memorymaster.dreaming import held
    from memorymaster.dreaming.consolidation_egress import WITHHELD_RATIONALE
    from memorymaster.dreaming.ledger import DreamLedger
    from memorymaster.dreaming.worker import DreamConfig, DreamWorker

    ledger = DreamLedger(tmp_path / "capture.db")
    capture_id = _capture(ledger)
    recorded: list = []
    monkeypatch.setattr(held, "triage_notes", lambda extraction: {"a": {"decision_id": "d"}, "b": {"decision_id": "d"}})
    monkeypatch.setattr(held, "record_verdicts", lambda decisions, cid, verdicts, **kw: recorded.extend(verdicts))
    worker = DreamWorker(
        ledger, _service(tmp_path), None, _Recorder(), config=DreamConfig(jev_triage_enabled=True), now=lambda: NOW
    )
    worker._record_verdicts(
        capture_id,
        [
            {"candidate_id": "a", "action": "ignore", "rationale": "model said no"},
            {"candidate_id": "b", "action": "ignore", "rationale": WITHHELD_RATIONALE},
        ],
    )
    assert [action for _note, action in recorded] == ["ignore"]  # only the model's own verdict


# ---- the gate itself: long runs, seams ------------------------------------------------------


def test_a_long_unbroken_run_is_replaced_but_the_rest_of_the_message_is_gated_normally():
    from memorymaster.core.provider_egress import LONG_TOKEN, export_text

    out = export_text("start " + "A" * 16_100 + f" tail fact {IP} end")
    assert out == f"start {LONG_TOKEN} tail fact [REDACTED:private_ipv4] end"


def test_timing_on_ordinary_prose_and_on_tokens_just_under_the_cap():
    import random
    import time

    from memorymaster.core.provider_egress import MAX_TOKEN, export_text

    rng = random.Random(3)
    prose = " ".join(rng.choice(["service", "retries", "deploy", "config", "host"]) for _ in range(30_000))[:200_000]
    tokens = " ".join("".join(rng.choice("abcdefghij0123456789") for _ in range(MAX_TOKEN - 1)) for _ in range(100))
    for text in (prose, tokens):
        started = time.monotonic()
        export_text(text)
        assert time.monotonic() - started < 3


@pytest.mark.parametrize("stored", [False, True], ids=["raw", "stored"])
def test_a_context_secret_is_never_cut_from_its_value_at_any_offset(stored):
    from memorymaster.core.provider_egress import export_messages, export_text

    value = "Tr0ub4dor77" + "Zx93Qv"
    shapes = [f"password: {value}", f"Authorization: Bearer {value}", f"secret token: {value}"]
    for offset in range(19_900, 20_101):
        for shape in shapes:
            text = ("lorem ipsum " * 2_000)[:offset] + " " + shape + " trailing words"
            out = export_messages([{"text": text}])[0][0]["text"] if stored else export_text(text)
            assert out is None or value not in out, (offset, shape)

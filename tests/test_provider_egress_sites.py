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
from test_provider_egress_gate import TOKEN, _run, BENIGN, BLOCKED, IP, MAIL, WIN_PATH, B64, _assert_clean

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


def test_contradiction_probe_skips_a_blocked_pair_and_masks_a_literal_credential(monkeypatch):
    from memorymaster.govern import contradiction_probe as probe

    seen: list[str] = []
    monkeypatch.setattr(probe.llm_provider, "call_llm", lambda prompt, body: seen.append(body) or "[]")
    assert probe._judge_llm(_claim("deploy " + BLOCKED), _claim(BENIGN)) is None
    assert seen == []
    probe._judge_llm(_claim("the key is " + TOKEN), _claim("the key is another"))
    assert len(seen) == 1 and TOKEN not in seen[0]  # substituted, as in the claim gate


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
    summary = DreamWorker(ledger, service, _Recorder(), consolidator, config=DreamConfig(), now=lambda: NOW).run(
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
        assert time.monotonic() - started < 4  # ~1.5 s measured; headroom for a loaded runner


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


# ---- follow-ups: pair blocking, trusted names, marker-only messages, marker-only quotes -----


def test_pair_with_a_home_path_or_a_private_ip_port_is_sent_not_skipped(monkeypatch):
    from memorymaster.govern import contradiction_probe as probe

    seen: list[str] = []
    monkeypatch.setattr(probe.llm_provider, "call_llm", lambda prompt, body: seen.append(body) or "[]")
    probe._judge_llm(
        _claim("The config lives in /home/jdoe/app.yml"), _claim("The config lives in /home/asmith/app.yml")
    )
    probe._judge_llm(_claim("The database listens on 10.1.2.3:5432"), _claim("The database listens on 10.9.8.7:5432"))
    assert len(seen) == 2
    assert "jdoe" not in seen[0] and "asmith" not in seen[0] and "#1" in seen[0] and "#2" in seen[0]
    assert "10.1.2.3" not in seen[1] and "10.9.8.7" not in seen[1]


def test_trusted_neutralisation_accepts_only_marker_names_our_gate_emits():
    from memorymaster.dreaming.consolidation_egress import gate_consolidation

    fake = [
        _candidate("akia", "x AKIA[REDACTED:x]IOSFODNN7EXAMPLE y"),
        _candidate("anthropic", "key sk-ant-api03-A1b2C3d4[REDACTED:x]E5f6G7h8I9j0K1l2M3n4O5p6 end"),
    ]
    sent, ignored, _refs, _n = gate_consolidation(fake, [])
    assert sent == [] and [d.candidate_id for d in ignored] == ["akia", "anthropic"]
    ours = [_candidate("ok", "The host [REDACTED:private_ipv4] is up and /home/x[REDACTED:home_path_unix]y")]
    assert [c.candidate_id for c in gate_consolidation(ours, [])[0]] == ["ok"]


def test_a_message_of_only_blobs_counts_as_withheld_and_a_window_of_them_makes_no_call(tmp_path):
    from memorymaster.core.provider_egress import LONG_TOKEN, export_messages

    safe, withheld = export_messages([{"message_id": "a", "text": "A" * 5_000}, {"message_id": "b", "text": BENIGN}])
    assert withheld == 1 and safe[0]["text"] == LONG_TOKEN and safe[1]["text"] == BENIGN
    summary, extractor, _ledger, _cid = _run(tmp_path, ["A" * 5_000, "B" * 5_000])
    assert extractor.calls == [] and summary["errors"] == 0 and summary["egress_withheld"] == 2


def test_a_quote_with_no_content_outside_markers_does_not_bind():
    from memorymaster.core.provider_egress import quote_in_sent_text

    text = f"The primary lives at {IP} in the lab"
    assert quote_in_sent_text("lives at [REDACTED:private_ipv4] in", text) is True
    assert quote_in_sent_text("[REDACTED:private_ipv4]", text) is False
    assert quote_in_sent_text("[WITHHELD:long_token]", "A" * 5_000) is False


# ---- the twelve formerly dormant call sites ---------------------------------------------------

HOME_POSIX = "/home/" + "jdoe" + "/proj/app.yml"
LEAK = f"host {IP} file {WIN_PATH} home {HOME_POSIX} mail {MAIL} key {TOKEN} and " + BENIGN
LEAK_B = f"replica {IP} file {WIN_PATH} home {HOME_POSIX} mail {MAIL} key {TOKEN} and " + BENIGN


def _spy(returns: str = "[]"):
    seen: list[str] = []

    def call(*args, **kwargs):
        seen.append("\n".join(str(a) for a in args))
        return returns

    return seen, call


def test_auto_resolver_gates_both_claims_and_citations_and_withheld_makes_no_call(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from memorymaster.govern import auto_resolver as ar

    def claim(i, text):
        cites = [SimpleNamespace(source="notes", locator=WIN_PATH)]
        return SimpleNamespace(id=i, text=text, confidence=0.5, updated_at="2026-01-01", citations=cites)

    seen, call = _spy("{}")
    monkeypatch.setattr(ar, "call_llm", call)
    ar.resolve_conflict_pair(MagicMock(), claim(1, LEAK), claim(2, LEAK_B))
    assert len(seen) == 1
    _assert_clean(seen[0])
    seen.clear()
    result = ar.resolve_conflict_pair(MagicMock(), claim(1, "deploy " + BLOCKED), claim(2, BENIGN))
    assert seen == [] and result == {"resolved": False, "reason": "egress_withheld"}


def test_compact_summaries_prompt_block_is_gated_line_by_line():
    from types import SimpleNamespace

    from memorymaster.govern.jobs import compact_summaries as cs

    def claim(text):
        return SimpleNamespace(subject="svc", predicate="uses", object_value=None, text=text)

    block = cs._build_claim_text_block([claim(LEAK), claim("deploy " + BLOCKED), claim(BENIGN)])
    _assert_clean(block)
    assert "hunter2" not in block and BENIGN in block and block.count("\n") == 1
    assert cs._build_claim_text_block([claim("deploy " + BLOCKED)]) == ""  # nothing left: the caller makes no call


@pytest.mark.parametrize("use_provider", [True, False], ids=["llm_provider", "direct_http"])
def test_llm_steward_extract_claim_gates_the_text_on_both_transports(monkeypatch, use_provider):
    from memorymaster.govern import llm_steward as ls

    seen, call = _spy("[]")
    monkeypatch.setattr("memorymaster.core.llm_provider.call_llm", call)
    monkeypatch.setattr(ls, "_call_llm", lambda provider, key, model, prompt, base_url="", **kw: call(prompt))
    result = ls.extract_claim("google", "k", "m", 1, LEAK, use_llm_provider=use_provider)
    assert len(seen) == 1 and result.original_text == LEAK
    _assert_clean(seen[0])
    seen.clear()
    withheld = ls.extract_claim("google", "k", "m", 1, "deploy " + BLOCKED, use_llm_provider=use_provider)
    assert seen == [] and withheld.action == "skip" and withheld.extractions == []  # never "archive"


def test_entity_extractor_llm_gates_the_snippet(monkeypatch):
    from memorymaster.knowledge import entity_extractor as ee

    monkeypatch.setenv("MEMORYMASTER_ENTITY_LLM", "1")
    seen, call = _spy("[]")
    monkeypatch.setattr("memorymaster.core.llm_provider.call_llm", call)
    ee.extract_llm(LEAK)
    assert len(seen) == 1
    _assert_clean(seen[0])
    seen.clear()
    assert ee.extract_llm("deploy " + BLOCKED) == [] and seen == []


def test_rule_miner_gates_the_window_and_a_withheld_one_is_no_correction_not_transient(monkeypatch):
    from memorymaster.knowledge import rule_miner as rm

    seen, call = _spy("[]")
    monkeypatch.setattr(rm.llm_provider, "call_llm", call)
    assert rm._extract_rule("ASSISTANT: " + LEAK + "\nUSER: no, use the other one") is None
    assert len(seen) == 1
    _assert_clean(seen[0])
    seen.clear()
    assert rm._extract_rule("USER: " + BLOCKED) is None and seen == []


def test_skills_review_gates_the_evidence_and_a_withheld_one_is_a_blocked_review(monkeypatch):
    from memorymaster.knowledge import skills

    seen, call = _spy("")
    monkeypatch.setattr(skills.llm_provider, "call_llm", call)
    monkeypatch.setattr(skills, "_existing_skill_summaries", lambda service, scope: [])
    monkeypatch.setattr(skills, "_record_review_diagnostic", lambda *a, **k: None)
    stats = {"llm_calls": 0, "blocked": 0, "errors": []}
    skills._review_one_input(None, {"claim_id": 1, "scope": "project:x", "text": LEAK}, stats)
    assert len(seen) == 1
    _assert_clean(seen[0])
    seen.clear()
    skills._review_one_input(None, {"claim_id": 2, "scope": "project:x", "text": "deploy " + BLOCKED}, stats)
    assert seen == [] and stats["blocked"] == 1


def test_vault_curator_gates_each_claim_and_skips_an_all_withheld_batch(monkeypatch):
    from memorymaster.knowledge import vault_curator as vc

    seen, call = _spy("[]")
    monkeypatch.setattr("memorymaster.core.llm_provider.call_llm", call)
    vc._batch_categorize([{"id": 1, "text": LEAK}, {"id": 2, "text": "deploy " + BLOCKED}])
    assert len(seen) == 1 and "ID=2" not in seen[0]
    _assert_clean(seen[0])
    seen.clear()
    assert vc._batch_categorize([{"id": 3, "text": "deploy " + BLOCKED}]) == {} and seen == []


def test_vault_linter_gates_the_compared_values_with_pseudonyms(monkeypatch):
    from memorymaster.knowledge import vault_linter as vl

    seen, call = _spy("[]")
    monkeypatch.setattr("memorymaster.core.llm_provider.call_llm", call)
    pair = {"key": "k1", "claims": [{"value": f"lives at {IP} now"}, {"value": "lives at 10.9.8.7 now"}]}
    vl._llm_verify_contradictions([pair])
    assert len(seen) == 1 and "#1" in seen[0] and "#2" in seen[0]
    _assert_clean(seen[0])
    seen.clear()
    blocked = {"key": "k2", "claims": [{"value": "deploy " + BLOCKED}, {"value": BENIGN}]}
    assert vl._llm_verify_contradictions([blocked]) == [blocked] and seen == []


def test_wiki_engine_wrapper_gates_prompt_and_text_for_every_call_site(monkeypatch):
    from memorymaster.core.provider_egress import export_stored
    from memorymaster.knowledge import wiki_engine as we

    seen, call = _spy("answer")
    monkeypatch.setattr("memorymaster.core.llm_provider.call_llm", call)
    assert we._call_llm("Rewrite this article:\n" + LEAK, LEAK_B) == "answer"
    _assert_clean(seen[0])
    seen.clear()
    assert we._call_llm("Rewrite:\n" + BLOCKED, "") == "" and seen == []
    assert we._call_llm("Rewrite", BLOCKED) == "" and seen == []
    for template in (we.ABSORB_PROMPT, we.CLEANUP_PROMPT, we.BREAKDOWN_PROMPT):
        assert export_stored(template) == template  # the instruction templates are not over-withheld


def test_llm_rerank_gates_query_and_snippets_and_a_withheld_request_charges_no_failure(monkeypatch):
    from memorymaster.recall import llm_rerank as lr

    seen, call = _spy('[{"candidate_index": 1, "relevance_score": 90}]')
    monkeypatch.setattr(lr, "call_llm", call)
    monkeypatch.setattr(lr, "_pace_judge_call", lambda: None)
    monkeypatch.setattr(lr, "_CONSECUTIVE_FAILURES", 0)
    monkeypatch.setattr(lr, "_DISABLED", False)
    lr.rerank_with_llm("where is the host " + IP, [LEAK, "deploy " + BLOCKED, BENIGN], top_k=2)
    assert len(seen) == 1
    _assert_clean(seen[0])
    assert "hunter2" not in seen[0] and BENIGN in seen[0]
    seen.clear()
    failures = lr.get_rerank_stats()["failures"]
    ordered = lr.rerank_with_llm("q " + BLOCKED, ["a", "b"], top_k=2)
    ordered_all = lr.rerank_with_llm("fine question", ["deploy " + BLOCKED], top_k=1)
    assert seen == [] and ordered == ["a", "b"] and ordered_all == ["deploy " + BLOCKED]
    assert lr.get_rerank_stats()["failures"] == failures and lr.get_rerank_stats()["disabled"] == 0


def test_selfcheck_question_generator_gates_the_claim(monkeypatch):
    from memorymaster.recall import selfcheck as sc

    seen, call = _spy('["q?"]')
    monkeypatch.setattr("memorymaster.core.llm_provider.call_llm", call)
    assert sc.llm_question_generator(LEAK, 3) == ["q?"]
    _assert_clean(seen[0])
    seen.clear()
    assert sc.llm_question_generator("deploy " + BLOCKED, 3) == [] and seen == []


def test_workflow_classification_gates_the_request_excerpt(tmp_path, monkeypatch):
    from memorymaster.workflow_intelligence import classification
    from memorymaster.workflow_intelligence.models import SessionRecord
    from memorymaster.workflow_intelligence.storage import WorkflowStore

    seen, call = _spy('{"task_category": "devops", "outcome": "unknown", "confidence": 0.7, "rationale": "x"}')
    monkeypatch.setattr(classification.llm_provider, "call_llm", call)
    store = WorkflowStore(tmp_path / "workflow.db")
    try:
        for sid, excerpt in (("s1", LEAK), ("s2", "deploy " + BLOCKED)):
            store.upsert_session(
                SessionRecord(
                    session_id=sid,
                    external_id=sid,
                    provider="codex",
                    session_kind="human",
                    project_scope="project:demo",
                    initial_request_excerpt=excerpt,
                ),
                source_file_id=None,
            )
        result = classification.classify_pending(store, limit=5)
    finally:
        store.close()
    assert len(seen) == 1 and result["classified"] == 1 and result["failures"] == 1
    _assert_clean(seen[0])

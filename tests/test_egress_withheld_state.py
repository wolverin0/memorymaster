"""A withheld input is "not judged": every site must end as if it had not seen the item.

No status change, no retirement, no failure/breaker/call counter, and no item that is re-selected forever.
Each test drives the site's real entry point with a stub provider that records what it was handed.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from test_recall_selfcheck import db  # noqa: F401  (fixture)
from test_rule_miner import _create_verbatim, _seed, env  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parents[1]
# Blocked by the egress gate, yet untouched by the literal sanitizers (a public address:port).
GATED = "The admin console listens on 203.0.113.9:8443 for the platform team"
BENIGN = "The compactor merges summaries older than the freshness window."


@pytest.fixture()
def selfcheck_db(db):  # noqa: F811
    return db


# ---- 1. llm_steward.run_steward --------------------------------------------------------------


def _steward_db(tmp_path: Path, texts: list[str]) -> Path:
    from memorymaster.core.models import CitationInput
    from memorymaster.core.service import MemoryService

    path = tmp_path / "steward.db"
    svc = MemoryService(str(path), workspace_root=tmp_path)
    svc.init_db()
    for i, text in enumerate(texts):
        svc.store.create_claim(text, [CitationInput(source="t")], scope="project:test", idempotency_key=f"k{i}")
    return path


@pytest.mark.parametrize("use_provider", [True, False], ids=["llm_provider", "direct_http"])
def test_run_steward_leaves_a_withheld_claim_a_candidate_and_makes_no_call_for_it(tmp_path, monkeypatch, use_provider):
    from memorymaster.govern import llm_steward as ls

    monkeypatch.delenv("QDRANT_URL", raising=False)
    path = _steward_db(tmp_path, [BENIGN + " one", GATED, BENIGN + " two", GATED + " again today"])
    prompts: list[str] = []

    def reply(*args, **kwargs):
        prompts.append(" ".join(str(a) for a in args))
        n = len(prompts)
        return json.dumps(
            [{"subject": f"s{n}", "predicate": f"p{n}", "object_value": "v", "action": "confirm", "confidence": 0.8}]
        )

    monkeypatch.setattr("memorymaster.core.llm_provider.call_llm", reply)
    monkeypatch.setattr(ls, "_call_llm", lambda provider, key, model, prompt, base_url="", **kw: reply(prompt))
    stats = ls.run_steward(
        str(path),
        api_key="x",
        provider="gemini",
        limit=100,
        delay=0.0,
        auto_validate=False,
        use_llm_provider=use_provider,
    )
    assert len(prompts) == 2 and not any("203.0.113.9" in p for p in prompts)
    assert stats["egress_withheld"] == 2 and stats["errors"] == 0
    conn = sqlite3.connect(path)
    try:
        status = dict(conn.execute("SELECT text, status FROM claims").fetchall())
        events = conn.execute("SELECT COUNT(*) FROM events WHERE details LIKE '%archiv%'").fetchone()[0]
    finally:
        conn.close()
    assert status[GATED] == "candidate" and status[GATED + " again today"] == "candidate"
    assert "archived" not in status.values() and events == 0


# ---- 2. compact_summaries.run ----------------------------------------------------------------


def test_compact_summary_links_only_the_claims_whose_lines_went_out(tmp_path):
    from memorymaster.core.lifecycle import transition_claim
    from memorymaster.core.models import CitationInput
    from memorymaster.govern.jobs.compact_summaries import _get_unsummarized_archived_claims, run
    from memorymaster.stores.storage import SQLiteStore

    store = SQLiteStore(tmp_path / "c.db")
    store.init_db()
    ids = []
    for i, text in enumerate([f"DNS record {i} points to cloudflare" for i in range(3)] + [GATED]):
        claim = store.create_claim(
            text=text, citations=[CitationInput(source="t")], subject="dns", predicate=f"p{i}", object_value=f"v{i}"
        )
        transition_claim(
            store,
            claim.id,
            to_status="confirmed",
            reason="t",
            event_type="transition",
            event_payload={"actor": "operator"},
        )
        transition_claim(store, claim.id, to_status="stale", reason="t", event_type="decay")
        transition_claim(store, claim.id, to_status="archived", reason="t", event_type="compactor")
        ids.append(claim.id)
    prompts: list[str] = []
    answer = json.dumps(
        {
            "summary_text": "DNS uses cloudflare records.",
            "subject": "dns",
            "predicate": "summary_of",
            "object_value": "cloudflare",
            "confidence": 0.9,
        }
    )
    with patch(
        "memorymaster.govern.jobs.compact_summaries._call_llm",
        side_effect=lambda *a, **k: prompts.append(a[3]) or answer,
    ):
        result = run(store, provider="gemini", api_key="k", min_cluster=3, dry_run=False)
    assert len(prompts) == 1 and "203.0.113.9" not in prompts[0]
    assert result.summaries_created == 1 and result.source_claims_summarized == 3
    summary = next(c for c in store.list_claims(status="confirmed") if c.claim_type == "summary")
    linked = {link.target_id for link in store.get_claim_links(summary.id) if link.link_type == "derived_from"}
    assert linked == set(ids[:3])  # the withheld claim is not covered by a summary that never saw it
    assert ids[3] in {c.id for c in _get_unsummarized_archived_claims(store, limit=50)}
    assert store.get_claim(ids[3]).status == "archived"


# ---- 3. classification -----------------------------------------------------------------------


def test_classify_pending_does_not_stall_on_withheld_rows_and_counts_no_failure(tmp_path, monkeypatch):
    from memorymaster.workflow_intelligence import classification
    from memorymaster.workflow_intelligence.models import SessionRecord
    from memorymaster.workflow_intelligence.storage import WorkflowStore

    calls: list[str] = []
    answer = '{"task_category": "devops", "outcome": "unknown", "confidence": 0.7, "rationale": "x"}'
    monkeypatch.setattr(classification.llm_provider, "call_llm", lambda p, t: calls.append(t) or answer)
    store = WorkflowStore(tmp_path / "workflow.db")
    try:
        rows = [(f"blocked-{i:02d}", GATED) for i in range(60)] + [
            (f"ok-{i}", "Debug the failing deployment") for i in range(5)
        ]
        for n, (sid, excerpt) in enumerate(rows):
            store.upsert_session(
                SessionRecord(
                    session_id=sid,
                    external_id=sid,
                    provider="codex",
                    session_kind="human",
                    project_scope="project:demo",
                    initial_request_excerpt=excerpt,
                    started_at=f"2026-01-01T00:{n // 60:02d}:{n % 60:02d}Z",
                ),
                source_file_id=None,
            )
        results = [classification.classify_pending(store, limit=50) for _ in range(4)]
        pending = store.connection.execute(
            "SELECT COUNT(*) FROM sessions WHERE classification_prompt_hash=''"
        ).fetchone()[0]
        categories = {r["session_id"]: r["task_category"] for r in store.session_rows()}
    finally:
        store.close()
    assert sum(r["classified"] for r in results) == 5 and sum(r["failures"] for r in results) == 0
    assert sum(r["withheld"] for r in results) == 60 and len(calls) == 5 and pending == 0
    assert all(categories[f"blocked-{i:02d}"] == "unknown" for i in range(60))  # nothing was invented for them


# ---- 4. selfcheck ----------------------------------------------------------------------------


def test_selfcheck_records_a_withheld_claim_as_skipped_sensitive_not_as_a_failure(selfcheck_db, tmp_path):
    from memorymaster.recall import selfcheck

    conn = sqlite3.connect(selfcheck_db)
    conn.execute("UPDATE claims SET text = ? WHERE text LIKE '%zebra%'", (GATED + " zebra",))
    conn.execute("UPDATE claims SET text = ? WHERE text LIKE '%lemur%'", (GATED + " lemur",))
    conn.commit()
    conn.close()
    seen: list[str] = []

    def generate(text, n):
        seen.append(text)
        return ["how long does quokka hold the lock", "quokka job details"][:n]

    run = selfcheck.run_selfcheck(selfcheck_db, sidecar_path=tmp_path / "side.sqlite", generate=generate)
    assert run.status == "ok" and run.claims_skipped_sensitive == 2 and run.claims_evaluated == 1
    assert run.llm_calls == 1 and not any("203.0.113.9" in s for s in seen)


# ---- 5. rule_miner ---------------------------------------------------------------------------


def test_mine_rules_counts_no_llm_call_for_a_withheld_window(env):  # noqa: F811
    from memorymaster.knowledge import rule_miner

    db, svc, holder = env
    _seed(
        db,
        [
            (1, "s1", "assistant", "I set the admin endpoint to 203.0.113.9:8443 in the config file directly."),
            (2, "s1", "user", "no, don't hardcode that endpoint like that, use an env var instead please."),
        ],
    )
    stats = rule_miner.mine_rules(db, svc, provider="claude_cli")
    assert holder["calls"] == 0 and stats["llm_calls"] == 0 and stats["skipped"] == 1 and stats["ingested"] == 0


def test_stop_hook_rule_mining_counts_no_llm_call_for_a_withheld_window(tmp_path, monkeypatch):
    from memorymaster.knowledge import rule_miner

    seen: list[str] = []
    monkeypatch.setattr(rule_miner.llm_provider, "call_llm", lambda p, w: seen.append(w) or "[]")
    window = rule_miner._build_window(
        "I set the endpoint to 203.0.113.9:8443 in the file.", "no, use an env var instead"
    )
    assert rule_miner._extract_rule(window) is None and seen == []


# ---- 6. sweep: every other site -------------------------------------------------------------


def test_auto_resolver_leaves_a_withheld_pair_conflicted_and_unevaluated():
    from memorymaster.govern import auto_resolver as ar

    def claim(i, text):
        return SimpleNamespace(
            id=i,
            text=text,
            confidence=0.5,
            updated_at="2026-01-01",
            citations=[],
            status="conflicted",
            subject="svc",
            predicate="uses",
            scope="project:t",
        )

    a, b = claim(1, GATED), claim(2, BENIGN)
    store = MagicMock()
    store.find_by_status.return_value = [a, b]
    store.get_claim.side_effect = lambda cid, **kw: {1: a, 2: b}[cid]
    with patch.object(ar, "call_llm", side_effect=AssertionError("no call may be made")):
        result = ar.auto_resolve_conflicts(store, limit=5)
    assert result == {"pairs_evaluated": 0, "resolved": 0, "failed": 0}
    assert not store.method_calls or all(c[0] in {"find_by_status", "get_claim"} for c in store.method_calls)


def test_wiki_absorb_single_claim_writes_nothing_and_binds_nothing_for_a_withheld_claim(tmp_path, monkeypatch):
    from memorymaster.core.models import CitationInput
    from memorymaster.core.service import MemoryService
    from memorymaster.knowledge import wiki_engine

    monkeypatch.delenv("QDRANT_URL", raising=False)
    svc = MemoryService(str(tmp_path / "w.db"), workspace_root=tmp_path)
    svc.init_db()
    claim = svc.store.create_claim(
        GATED, [CitationInput(source="t")], scope="project:t", subject="console", idempotency_key="w1"
    )
    monkeypatch.setattr(
        "memorymaster.core.llm_provider.call_llm",
        lambda *a, **k: pytest.fail("a withheld claim must not reach the provider"),
    )
    wiki = tmp_path / "wiki"
    result = wiki_engine.absorb_single_claim(claim.id, db_path=tmp_path / "w.db", wiki_dir=wiki)
    assert result == {"claim_id": claim.id, "absorbed": False, "reason": "egress_withheld"}
    assert not list(wiki.rglob("*.md"))
    conn = sqlite3.connect(tmp_path / "w.db")
    try:
        row = conn.execute("SELECT * FROM claims WHERE id=?", (claim.id,)).fetchone()
        cols = [d[0] for d in conn.execute("SELECT * FROM claims").description]
    finally:
        conn.close()
    assert not dict(zip(cols, row)).get("wiki_article")  # not bound to an article


def test_skills_withheld_item_is_closed_as_not_judged_without_a_failure(monkeypatch):
    from memorymaster.knowledge import skills

    outcomes: list[str] = []
    monkeypatch.setattr(skills.llm_provider, "call_llm", lambda *a, **k: pytest.fail("no call"))
    monkeypatch.setattr(skills, "_existing_skill_summaries", lambda service, scope: [])
    monkeypatch.setattr(skills, "_record_review_completion", lambda service, cid, outcome: outcomes.append(outcome))
    stats = {"llm_calls": 0, "blocked": 0, "errors": []}
    skills._review_one_input(None, {"claim_id": 7, "scope": "project:x", "text": GATED}, stats)
    assert (
        outcomes == ["egress_withheld"] and stats["errors"] == [] and stats["blocked"] == 0 and stats["llm_calls"] == 0
    )


def test_vault_curator_files_a_withheld_claim_like_any_uncategorised_one(monkeypatch):
    from memorymaster.knowledge import vault_curator as vc

    monkeypatch.setattr("memorymaster.core.llm_provider.call_llm", lambda *a, **k: "[]")
    categories = vc._batch_categorize([{"id": 1, "text": GATED}, {"id": 2, "text": BENIGN}])
    tree = vc._group_by_scope_and_topic([{"id": 1, "scope": "project:t", "text": GATED}], categories)
    assert 1 not in categories and list(next(iter(tree.values()))) == [
        "other"
    ]  # same as an omitted one; nothing stored


def test_vault_linter_leaves_a_withheld_contradiction_unverified(monkeypatch):
    from memorymaster.knowledge import vault_linter as vl

    monkeypatch.setattr("memorymaster.core.llm_provider.call_llm", lambda *a, **k: pytest.fail("no call"))
    contradiction = {"key": "k", "claims": [{"value": GATED}, {"value": BENIGN}]}
    out = vl._llm_verify_contradictions([contradiction])
    assert out == [{"key": "k", "claims": [{"value": GATED}, {"value": BENIGN}]}] and "verified" not in out[0]


def test_rerank_withheld_request_keeps_input_order_and_charges_no_failure(monkeypatch):
    from memorymaster.recall import llm_rerank as lr

    monkeypatch.setattr(lr, "call_llm", lambda *a, **k: pytest.fail("no call"))
    monkeypatch.setattr(lr, "_CONSECUTIVE_FAILURES", 0)
    monkeypatch.setattr(lr, "_DISABLED", False)
    before = dict(lr.get_rerank_stats())
    assert lr.rerank_with_llm("where is the console", [GATED, GATED + " b"], top_k=2) == [GATED, GATED + " b"]
    after = lr.get_rerank_stats()
    assert after["failures"] == before["failures"] and after["attempts"] == before["attempts"] and not after["disabled"]


def test_entity_extractor_withheld_text_has_no_state_effect(monkeypatch):
    from memorymaster.knowledge import entity_extractor as ee

    monkeypatch.setenv("MEMORYMASTER_ENTITY_LLM", "1")
    monkeypatch.setattr("memorymaster.core.llm_provider.call_llm", lambda *a, **k: pytest.fail("no call"))
    assert ee.extract_llm(GATED) == []


# ---- 7. the judge scripts ---------------------------------------------------------------------


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name + "_t", ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_judge_claim_value_treats_a_gate_blocked_claim_as_flagged_and_never_sends_it():
    module = _load("judge_claim_value")
    assert module.is_flagged({"text": GATED}) is True
    assert module.is_flagged({"text": BENIGN}) is False
    prompt = module.build_prompt([{"text": "host 10.1.2.3 is run by ops@example.org", "scope": "p"}])
    assert "10.1.2.3" not in prompt and "ops@example.org" not in prompt


def test_label_prompts_judge_gates_the_prompt_and_the_snippets(monkeypatch):
    module = _load("label_prompts_with_judge")
    sent: list[tuple] = []
    monkeypatch.setattr(
        module,
        "_get_candidates",
        lambda db, text, k: [
            {"id": 1, "text": "console at 10.1.2.3 owned by ops@example.org"},
            {"id": 2, "text": GATED},
        ],
    )
    monkeypatch.setattr(module, "_call_judge", lambda text, cands, **kw: sent.append((text, cands)) or ([1], {}))
    args = SimpleNamespace(db="x", top_k=5, judge_provider="claude_cli")
    ids, _prov = module._label_one(args, None, "where is 10.1.2.3 and who is ops@example.org")
    assert ids == [1] and len(sent) == 1
    blob = json.dumps(sent)
    assert "10.1.2.3" not in blob and "ops@example.org" not in blob and "203.0.113.9" not in blob
    assert module._label_one(args, None, GATED) == ([], {"status": "skipped", "reason": "egress_withheld"})


# ---- 8. trusted mode: a marker spliced into a token -----------------------------------------


def test_trusted_mode_blocks_a_credential_with_a_marker_spliced_into_its_middle():
    from memorymaster.core.provider_egress import export_stored

    assert export_stored("x AKIA[REDACTED:private_ip]IOSFODNN7EXAMPLE y", trusted=True) is None
    assert export_stored("k sk-ant-api03-A1b2C3d4[REDACTED:private_ip]E5f6G7h8I9j0K1l2M3n4O5p6", trusted=True) is None
    assert export_stored("at /home/x[REDACTED:home_path_unix]proj/app.yml is up", trusted=True) is not None

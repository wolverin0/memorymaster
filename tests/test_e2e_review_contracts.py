"""Proposed acceptance contracts; intentionally red on the reviewed source."""
import gc
import json
import runpy
from datetime import timedelta

from memorymaster.dreaming.ledger import DreamLedger
from memorymaster.dreaming.worker import DreamConfig, DreamWorker
from memorymaster.recall.context_optimizer import estimate_tokens, pack_context
from memorymaster.surfaces.dashboard_auth import check_csrf


def test_deferred_consolidation_resumes_next_day_without_reextracting(tmp_path):
    helper = runpy.run_path("tests/test_dreaming_worker.py")
    service = helper["_service"](tmp_path)
    ledger = DreamLedger(tmp_path / "capture.db")
    capture_id = helper["_capture"](ledger)
    first = DreamWorker(ledger, service, helper["_Extractor"](), helper["_Consolidator"](),
                       config=DreamConfig(max_consolidate_calls_daily=0),
                       now=lambda: helper["NOW"]).run(apply_candidates=True)
    second = DreamWorker(ledger, service, helper["_NoCall"](), helper["_Consolidator"](),
                        config=DreamConfig(max_consolidate_calls_daily=12),
                        now=lambda: helper["NOW"] + timedelta(days=1)).run(apply_candidates=True)
    observed = {"first_deferred": first["deferred_consolidate_budget"],
                "second_ok": second["ok"], "second_applied": second["applied"],
                "second_errors": second["errors"], "state": ledger.get_capture(capture_id)["state"]}
    del service, ledger
    gc.collect()
    assert first["deferred_consolidate_budget"] == 1
    assert observed["second_applied"] == 1, observed
    assert observed["state"] == "applied"


def test_json_budget_measures_serialized_output():
    helper = runpy.run_path("tests/test_context_optimizer.py")
    rows = [helper["_make_row"](helper["_make_claim"](
        id=i, text=("synthetic fact " * 15) + str(i)), score=1 - i / 100)
        for i in range(1, 7)]
    result = pack_context(rows, token_budget=256, output_format="json")
    assert estimate_tokens(result.output) <= result.token_budget


def test_packed_rows_match_rendered_claim_ids():
    helper = runpy.run_path("tests/test_context_optimizer.py")
    rows = [helper["_make_row"](helper["_make_claim"](
        id=i, text=("synthetic fact " * 15) + str(i))) for i in range(1, 7)]
    result = pack_context(rows, token_budget=256, output_format="json")
    assert [row["claim"].id for row in result.rows] == [item["id"] for item in json.loads(result.output)["claims"]]


def test_csrf_rejects_lookalike_hostname(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_DASHBOARD_TOKEN_OPERATOR", "fixture-only")
    decision = check_csrf({"Origin": "https://evil-localhost:8765"},
                          configured_host_port="localhost:8765")
    assert not decision.ok

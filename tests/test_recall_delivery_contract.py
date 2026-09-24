"""The receipt describes only delivered memory; context budgets include framing."""

import json
import runpy
from dataclasses import replace
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest

from memorymaster.knowledge import context_bundle
from memorymaster.public.v1 import RecallReceipt
from memorymaster.recall.context_optimizer import estimate_tokens, pack_context

H = runpy.run_path("tests/test_context_optimizer.py")


@pytest.mark.parametrize("fmt", ["text", "xml", "json"])
@pytest.mark.parametrize("budget", [1, 48, 64, 128, 256, 4096])
def test_serialized_budget_metadata_and_ids(fmt, budget):
    rows = [H["_make_row"](H["_make_claim"](id=i, text=("Ñandú 中文 <&> 🚀 " * i))) for i in range(1, 8)]
    try:
        result = pack_context(rows, token_budget=budget, output_format=fmt)
    except ValueError as error:
        assert "minimum" in str(error)
        assert budget <= 64
        return
    assert result.tokens_used == estimate_tokens(result.output) <= budget
    selected = [row["claim"].id for row in result.rows]
    assert len(selected) == result.claims_included
    if fmt == "json":
        data = json.loads(result.output)
        assert selected == [claim["id"] for claim in data["claims"]]
        assert data["meta"]["tokens_used"] == result.tokens_used
    elif fmt == "xml":
        root = ElementTree.fromstring(result.output)
        assert int(root.find("meta").attrib["tokens_used"]) == result.tokens_used
        assert selected == [int(claim.attrib["id"]) for claim in root.findall("claim")]
    else:
        assert f"{result.tokens_used}/{budget} tokens" in result.output
        for row in rows:
            assert (f"id={row['claim'].id} " in result.output) == (row["claim"].id in selected)


def test_derived_rows_filtered_before_any_budget_is_spent(monkeypatch):
    ordinary = H["_make_row"](H["_make_claim"](id=3, text="SQLite uses WAL."))
    rows = [
        H["_make_row"](replace(H["_make_claim"](id=1, text="x" * 900), claim_type="observation")),
        H["_make_row"](replace(H["_make_claim"](id=2, text="y" * 900), claim_type="skill")),
        ordinary,
    ]
    requests = []

    def retrieve(request):
        requests.append(request)
        return SimpleNamespace(rows=rows)

    monkeypatch.setattr(context_bundle, "GraphObservationRepository", lambda _store: SimpleNamespace(scope_observations=lambda **_kw: []))
    service = SimpleNamespace(store=object(), retrieve=retrieve)
    result = context_bundle.query_context_bundle(service, "SQLite", scope_allowlist=["project:test"], token_budget=128)
    assert [row["claim"].id for row in result.rows] == [3]
    assert requests[0].scope_allowlist == ("project:test",)
    assert requests[0].trust_mode == "trusted"
    assert "SQLite uses WAL." in result.output
    assert result.tokens_used == estimate_tokens(result.output) <= 128


def test_receipt_estimate_is_canonical_application_json():
    receipt = RecallReceipt(api_version="memorymaster.public.v1", output="Ñ 🚀 memory", claims=(),
                            token_budget=64, tokens_used=3, trust_mode="trusted", output_format="text")
    payload = receipt.to_dict()
    assert payload["budget_scope"] == "context"
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert payload["receipt_tokens_estimate"] == estimate_tokens(canonical)

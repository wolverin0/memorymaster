"""Disposable public demo acceptance contracts.

The demo must exercise capture, deterministic local processing, governed
promotion, trusted recall with citations, and supported graph traversal while
leaving no persistent database behind.
"""

from __future__ import annotations

import json

from memorymaster.public.demo import run_disposable_demo
from memorymaster.surfaces.cli import main


def test_disposable_demo_covers_public_lifecycle() -> None:
    report = run_disposable_demo()

    assert report["temporary_database_disposed"] is True
    assert report["captures"] == 2
    assert report["fixture_jobs_completed"] == 2
    assert report["candidate_claims_created"] == 2
    assert report["promoted_claim_id"] in report["recall_claim_ids"]
    assert report["recall_citations"]
    assert report["graph_paths"][0]["relation"] == "participates_in"


def test_demo_cli_emits_versioned_json(capsys) -> None:
    assert main(["--json", "demo"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["api_version"] == "memorymaster.public.v1"
    assert payload["temporary_database_disposed"] is True

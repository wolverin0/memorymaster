"""Transcript miner must use the FULL canonical secret scan, not the literal one.

WHY (P3 filter-bypass hardening): transcript_miner raw-INSERTs mined assistant
text into the claims table. It already gated on sensitivity, but via
``redact_text`` — a literal pattern pass that misses base64/hex/confusable-
encoded secrets. A credential that an assistant pasted base64-wrapped into a
transcript would therefore be persisted verbatim. The fix routes the gate
through ``scan_text_for_findings`` (the same decoded-variant sweep
``sanitize_claim_input`` runs at ``service.ingest``). These tests anchor on the
invariant — an encoded secret never reaches ``claims.text`` — not on the
implementation.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

from memorymaster.knowledge.transcript_miner import _contains_sensitive, mine_transcript
from memorymaster.service import MemoryService

_FAKE_KEY = "sk-ant-api03-NOTAREALKEY000000000000000000000000abcdefghijkl"


def test_contains_sensitive_catches_base64_encoded_secret() -> None:
    enc = base64.b64encode(_FAKE_KEY.encode()).decode()
    assert _contains_sensitive(_FAKE_KEY) is True
    assert _contains_sensitive(f"the token, base64-wrapped, is {enc}") is True
    assert _contains_sensitive("the root cause was a missing FK index") is False


def test_mine_transcript_does_not_persist_encoded_credential(tmp_path: Path) -> None:
    db = tmp_path / "miner.db"
    svc = MemoryService(db, workspace_root=tmp_path)
    svc.init_db()

    enc = base64.b64encode(_FAKE_KEY.encode()).decode()
    # A "valuable"-looking assistant message (matches VALUABLE_PATTERNS via
    # "the root cause was") that also carries a base64-wrapped credential.
    poisoned = (
        "The root cause was the API rejecting us; the working token, "
        f"base64-encoded, is {enc} and must be set in the env."
    )
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        json.dumps({"role": "assistant", "content": poisoned}) + "\n",
        encoding="utf-8",
    )

    mine_transcript(str(transcript), str(db), scope="project:test", min_length=10)

    # The encoded secret must never reach the claims table.
    with svc.store.connect() as conn:
        rows = conn.execute("SELECT text FROM claims").fetchall()
    assert all(enc not in (r[0] or "") for r in rows), "encoded secret persisted to claims"
    assert all(_FAKE_KEY not in (r[0] or "") for r in rows)

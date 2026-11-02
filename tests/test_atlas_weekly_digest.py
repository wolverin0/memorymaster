"""The weekly digest script: date filter, message build and public-safety, all without network."""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "atlas_weekly_digest.py"


@pytest.fixture
def digest(monkeypatch):
    for name in ("ATLAS_VM", "GEMINI_API_KEY", "MEMORYMASTER_DB"):
        monkeypatch.delenv(name, raising=False)
    spec = importlib.util.spec_from_file_location("atlas_weekly_digest_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # must import with no Gemini key and no ATLAS_VM
    return module


def _brain(path: Path) -> Path:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE claims (claim_type TEXT, subject TEXT, text TEXT, event_time TEXT, "
                "source_agent TEXT, created_at TEXT)")
    rows = [
        ("event", "recent", "dentist on friday", "2026-11-06", "atlas-llm-extractor", "datetime('now','-2 days')"),
        ("fact", "old", "paid a bill last month", None, "atlas-llm-extractor", "datetime('now','-20 days')"),
        ("event", "other", "not from atlas", None, "someone-else", "datetime('now','-1 days')"),
    ]
    for ct, subj, text, et, agent, created in rows:
        con.execute(f"INSERT INTO claims VALUES (?,?,?,?,?,{created})", (ct, subj, text, et, agent))
    con.commit()
    con.close()
    return path


def test_recent_claims_keeps_only_the_window_and_the_atlas_extractor(digest, tmp_path):
    db = _brain(tmp_path / "brain.db")
    assert [r[1] for r in digest.recent_claims(db, days=8)] == ["recent"]
    assert {r[1] for r in digest.recent_claims(db, days=30)} == {"recent", "old"}


def test_prompt_carries_today_and_the_claims_and_the_message_is_bounded(digest, tmp_path):
    rows = digest.recent_claims(_brain(tmp_path / "brain.db"), days=8)
    prompt = digest.build_prompt(rows, "01/11/2026")
    assert "HOY es 01/11/2026" in prompt and "(event) recent: dentist on friday [2026-11-06]" in prompt
    assert "paid a bill" not in prompt
    msg = digest.telegram_message("x" * 9000, today="01/11/2026")
    assert msg.startswith("<b>\U0001f9e0 Tu semana — 01/11/2026</b>") and len(msg) == 4000


def test_sending_without_a_relay_host_fails_loudly_instead_of_guessing_one(digest):
    with pytest.raises(SystemExit, match="ATLAS_VM"):
        digest.deliver_telegram("hola")


@pytest.mark.parametrize("model,expected", [
    ("gemini-3.1-flash-lite", {"thinkingLevel": "minimal"}),  # the model the scheduled wrapper sets
    ("gemini-flash-latest", None),  # the non-lite alias answers HTTP 400 to MINIMAL
])
def test_synthesize_uses_the_shared_thinking_guard(digest, monkeypatch, model, expected):
    """L21: the inline guard missed the versioned lite model and sent MINIMAL to the non-lite alias."""
    import json

    sent = {}

    class _Resp:
        def read(self):
            return b'{"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}'

    def fake_urlopen(req, timeout=0):
        sent["gen"] = json.loads(req.data)["generationConfig"]
        return _Resp()

    monkeypatch.setattr(digest, "MODEL", model)
    monkeypatch.setattr(digest, "_load_gemini_key", lambda: "k")
    monkeypatch.setattr(digest.urllib.request, "urlopen", fake_urlopen)
    digest.synthesize([])
    assert sent["gen"].get("thinkingConfig") == expected


def test_a_failed_delivery_is_not_reported_as_success(digest, monkeypatch, tmp_path):
    """L22: returncode was ignored and curl ran without -f, so a hidden task looked fine."""
    import subprocess

    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 22, "", "curl: (22) HTTP 500")

    monkeypatch.setattr(digest, "VM", "relay-host")
    monkeypatch.setattr(digest, "OUTDIR", str(tmp_path))
    monkeypatch.setattr(digest.subprocess, "run", fake_run)
    monkeypatch.setattr(digest, "recent_claims", lambda: [("event", "s", "t", None)])
    monkeypatch.setattr(digest, "synthesize", lambda rows: "digest")
    monkeypatch.setattr(digest, "curation_tally", lambda: "")
    monkeypatch.setattr(digest.sys, "argv", ["digest"])
    monkeypatch.delenv("DIGEST_NO_SEND", raising=False)
    assert digest.main() != 0
    assert "curl -sf" in seen["cmd"][-1]


def test_the_script_names_no_maintainer_address_or_home():
    source = SCRIPT.read_text(encoding="utf-8")
    assert "192.168." not in source and "_OneDrive" not in source and "pauol" not in source

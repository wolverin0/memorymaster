"""S6 HINTS: the classify hook asks Jev seven nouls; the regex stays the fallback.

The off-mode output must stay byte-identical to the regex-only hook (golden
outputs captured from the 30dbce2 template), live mode shows the labels at or
above their ``show`` threshold, and every failure (timeout, HTTP error,
malformed answer, missing key) degrades to the regex hints.  No request ever
reaches the provider: live/shadow runs use an injected fake transport.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from memorymaster.decisions.config import DecisionConfig
from memorymaster.decisions.credentials import ApiKey
from memorymaster.decisions.engine import DecisionEngine
from memorymaster.decisions.ledger import DecisionLedger
from memorymaster.decisions.questions import HINT_LABELS
from memorymaster.decisions.transport import ParsedAnswer, TransportResult

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "memorymaster" / "config_templates" / "hooks" / "memorymaster-classify.py"
GOLDEN = json.loads((ROOT / "tests" / "fixtures" / "classify_hook_off_golden.json").read_text(encoding="utf-8"))
KEY = ApiKey("ts-test-" + "H" * 24)  # synthetic
DECIDED = GOLDEN["cases"][0]  # "We decided to use WAL mode for the ledger." -> regex [DECISION]
NO_REGEX_MATCH = "I prefer short answers with code over explanations"


def _load_hook():
    spec = importlib.util.spec_from_file_location("mm_classify_hook_jev", HOOK)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def hook(monkeypatch):
    module = _load_hook()
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(module, "_log", lambda event, **kw: events.append((event, kw)))
    module.events = events
    return module


class FakeTransport:
    """Answers every hint noul from ``values`` (default 0.1); never touches the network."""

    def __init__(self, values: dict[str, float] | None = None, outcome: str = "ok", delay: float = 0.0):
        self.values = values or {}
        self.outcome = outcome
        self.delay = delay
        self.calls: list[dict[str, Any]] = []

    def send(self, payload, *, expected, timeout_s, max_retries=0):
        self.calls.append({"payload": payload, "expected": expected, "timeout_s": timeout_s,
                           "max_retries": max_retries})
        if self.delay:
            time.sleep(self.delay)
        if self.outcome != "ok":
            return TransportResult(429 if self.outcome == "http_429" else None, None, 5, 1, self.outcome)
        answers = {}
        for wire_id in expected:
            value = self.values.get(wire_id.split(".", 1)[1], 0.1)
            answers[wire_id] = ParsedAnswer("noul", value, {"noul": value})
        return TransportResult(200, {"model": "jev-1.13.0"}, 440, 1, "ok", model_served="jev-1.13.0",
                               tokens_in=446, tokens_out=72, cost_usd=446 * 0.042e-6, answers=answers)

    def close(self):
        pass


def make_engine(tmp_path: Path, transport: FakeTransport, *, mode: str = "live", key: ApiKey | None = KEY,
                env: dict[str, str] | None = None) -> DecisionEngine:
    # A hook deadline no loaded CI runner reaches unless the test is about the deadline
    # (CI 2026-09-24 fell back at the 900 ms default); deadline tests pass their own.
    environ = {"MEMORYMASTER_JEV_MODE": mode, "MEMORYMASTER_DECISIONS_DB": str(tmp_path / "decisions.db"),
               "MEMORYMASTER_JEV_HOOK_DEADLINE_MS": "10000"}
    environ.update(env or {})
    return DecisionEngine(DecisionConfig.from_env(environ), transport_factory=lambda _key: transport,
                          key_lookup=lambda: key)


def rows(tmp_path: Path, sql: str) -> list[dict[str, Any]]:
    return DecisionLedger(tmp_path / "decisions.db").query(sql)


def hint_names(stdout: str | None) -> list[str]:
    if not stdout:
        return []
    context = json.loads(stdout)["hookSpecificOutput"]["additionalContext"]
    return [line.split("]", 1)[0][3:] for line in context.splitlines() if line.startswith("- [")]


# ------------------------------------------------------------- off mode ---

def _installed_copy(tmp_path: Path) -> Path:
    """The hook as setup_hooks installs it (placeholder -> this checkout, forward slashes)."""
    target = tmp_path / "hooks" / HOOK.name
    target.parent.mkdir(parents=True, exist_ok=True)
    text = HOOK.read_text(encoding="utf-8").replace("__MEMORYMASTER_PROJECT_ROOT__", str(ROOT).replace("\\", "/"))
    target.write_text(text, encoding="utf-8")
    return target


@pytest.mark.parametrize("jev_env", [
    {},
    {"MEMORYMASTER_JEV_MODE": "off"},
    {"MEMORYMASTER_JEV_MODE": "live", "MEMORYMASTER_JEV_HINTS": "off"},
], ids=["unset", "global-off", "hints-off"])
def test_off_mode_output_is_byte_identical_to_the_regex_hook(jev_env, tmp_path) -> None:
    installed = _installed_copy(tmp_path)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MEMORYMASTER_", "TYPESAFE_"))}
    env.update(USERPROFILE=str(tmp_path), HOME=str(tmp_path),
               MEMORYMASTER_DECISIONS_DB=str(tmp_path / "decisions.db"), **jev_env)
    for case in GOLDEN["cases"]:
        proc = subprocess.run([sys.executable, str(installed)], input=case["stdin"].encode("utf-8"),
                              capture_output=True, env=env, timeout=60)
        assert proc.stdout == case["stdout"].encode("utf-8"), case["stdin"]
        assert proc.returncode == case["returncode"], case["stdin"]
    assert not (tmp_path / "decisions.db").exists()  # off logs nothing unless LOG_OFF is set


def test_installed_hook_loads_the_engine_and_logs_off_decisions_on_request(tmp_path) -> None:
    installed = _installed_copy(tmp_path)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MEMORYMASTER_", "TYPESAFE_"))}
    env.update(USERPROFILE=str(tmp_path), HOME=str(tmp_path), MEMORYMASTER_JEV_MODE="off",
               MEMORYMASTER_DECISIONS_LOG_OFF="1", MEMORYMASTER_DECISIONS_DB=str(tmp_path / "decisions.db"))
    decided = 0
    for case in GOLDEN["cases"]:
        proc = subprocess.run([sys.executable, str(installed)], input=case["stdin"].encode("utf-8"),
                              capture_output=True, env=env, timeout=60)
        assert proc.stdout == case["stdout"].encode("utf-8"), case["stdin"]
        try:
            prompt = json.loads(case["stdin"] or "{}").get("prompt")
        except ValueError:
            prompt = None
        decided += isinstance(prompt, str) and len(prompt) >= 5
    logged = rows(tmp_path, "SELECT surface, mode, fallback_reason, transport_outcome FROM decisions")
    assert len(logged) == decided == 9
    assert {tuple(r.values()) for r in logged} == {("hints", "off", "mode_off", "not_sent")}


def test_nothing_configured_is_off_without_loading_the_engine(hook) -> None:
    # The hook's no-import fast path relies on the code default being ``off``.
    assert DecisionConfig.from_env({}).mode_for("hints") == "off"
    assert hook.jev_configured({}) is False
    assert hook.jev_configured({"MEMORYMASTER_JEV_MODE": ""}) is False
    assert hook.jev_configured({"MEMORYMASTER_JEV_MODE": "live"}) is True
    assert hook.jev_configured({"MEMORYMASTER_JEV_HINTS": "shadow"}) is True
    assert hook.jev_configured({"MEMORYMASTER_DECISIONS_LOG_OFF": "1"}) is True


# ------------------------------------------------------------ live mode ---

def test_live_mode_sends_one_request_of_seven_nouls_over_the_redacted_prompt(hook, tmp_path) -> None:
    fake = FakeTransport({"decision": 0.9, "environment": 0.8})
    prompt = "We decided to keep jane.doe@example.com's box at 192.168.1.10 on WAL"

    out = hook.run({"prompt": prompt, "session_id": "sess-1"}, engine=make_engine(tmp_path, fake))

    assert hint_names(out) == ["DECISION", "ENVIRONMENT"]
    assert len(fake.calls) == 1
    payload = fake.calls[0]["payload"]
    assert set(payload["questions"]) == {f"hints.{label}" for label in HINT_LABELS}
    assert {q["type"] for q in payload["questions"].values()} == {"noul"}
    # hook kind: the time left of the 0.9 s deadline, no retries
    # hook kind: no retries (batch retries 3 times), at most the time left of the hook deadline
    assert 0 < fake.calls[0]["timeout_s"] <= 10.0 and fake.calls[0]["max_retries"] == 0
    sent = json.dumps(payload)
    assert "jane.doe@example.com" not in sent and "192.168.1.10" not in sent
    assert "[REDACTED:email]" in payload["state"]["prompt"]

    decision = rows(tmp_path, "SELECT * FROM decisions")[0]
    assert decision["surface"] == "hints" and decision["mode"] == "live" and decision["fallback_reason"] is None
    # F-11: the ledger joins sessions on the normalized, tenant-bound hash, never the raw id.
    from memorymaster.recall.jev_surfaces import decision_session_key

    assert decision["session_key"] == decision_session_key("sess-1")
    assert json.loads(decision["legacy_action"]) == ["hint:decision"]
    assert json.loads(decision["action_taken"]) == ["hint:decision", "hint:environment"]
    answered = rows(tmp_path, "SELECT question_id FROM decision_items WHERE question_id != ''")
    assert sorted(r["question_id"] for r in answered) == sorted(f"hints.{label}" for label in HINT_LABELS)
    exposed = rows(tmp_path, "SELECT item_ref FROM decision_items WHERE exposed = 1 ORDER BY item_ref")
    assert [r["item_ref"] for r in exposed] == ["hint:decision", "hint:environment"]


def test_labels_at_or_above_the_threshold_are_shown_even_without_a_regex_match(hook, tmp_path) -> None:
    fake = FakeTransport({"preference": 0.7, "decision": 0.69})

    out = hook.run({"prompt": NO_REGEX_MATCH}, engine=make_engine(tmp_path, fake))

    assert hint_names(out) == ["PREFERENCE"]
    assert "claim_type='preference'" in json.loads(out)["hookSpecificOutput"]["additionalContext"]


def test_live_mode_with_no_label_over_threshold_shows_nothing(hook, tmp_path) -> None:
    out = hook.run(json.loads(DECIDED["stdin"]), engine=make_engine(tmp_path, FakeTransport()))
    assert out is None
    assert json.loads(rows(tmp_path, "SELECT action_taken FROM decisions")[0]["action_taken"]) == []


# ------------------------------------------------------------ fallbacks ---

def test_timeout_degrades_to_the_regex_hints_within_the_deadline(hook, tmp_path) -> None:
    engine = make_engine(tmp_path, FakeTransport({"preference": 0.99}, delay=2.0),
                         env={"MEMORYMASTER_JEV_HOOK_DEADLINE_MS": "900"})  # the production hook deadline

    started = time.monotonic()
    out = hook.run(json.loads(DECIDED["stdin"]), engine=engine)
    elapsed = time.monotonic() - started

    assert out == DECIDED["stdout"]
    assert elapsed < 1.5, elapsed
    assert rows(tmp_path, "SELECT fallback_reason FROM decisions")[0]["fallback_reason"] == "timeout"


@pytest.mark.parametrize("outcome", ["http_429", "http_5xx", "malformed", "network_error"])
def test_transport_failures_degrade_to_the_regex_hints(outcome, hook, tmp_path) -> None:
    out = hook.run(json.loads(DECIDED["stdin"]), engine=make_engine(tmp_path, FakeTransport(outcome=outcome)))

    assert out == DECIDED["stdout"]
    assert rows(tmp_path, "SELECT fallback_reason FROM decisions")[0]["fallback_reason"] == outcome


def test_missing_key_degrades_to_the_regex_hints_and_is_logged(hook, tmp_path) -> None:
    fake = FakeTransport({"preference": 0.99})
    out = hook.run(json.loads(DECIDED["stdin"]), engine=make_engine(tmp_path, fake, key=None))

    assert out == DECIDED["stdout"] and fake.calls == []
    assert rows(tmp_path, "SELECT fallback_reason FROM decisions")[0]["fallback_reason"] == "missing_key"


def test_shadow_mode_keeps_the_regex_output_and_logs_jevs_labels(hook, tmp_path) -> None:
    fake = FakeTransport({"preference": 0.95})
    out = hook.run(json.loads(DECIDED["stdin"]), engine=make_engine(tmp_path, fake, mode="shadow"))

    assert out == DECIDED["stdout"]
    decision = rows(tmp_path, "SELECT * FROM decisions")[0]
    assert decision["mode"] == "shadow"
    assert json.loads(decision["jev_action"]) == ["hint:preference"]
    assert json.loads(decision["action_taken"]) == ["hint:decision"]


def test_short_prompts_are_never_sent(hook, tmp_path) -> None:
    fake = FakeTransport({"preference": 0.99})
    assert hook.run({"prompt": "hey"}, engine=make_engine(tmp_path, fake)) is None
    assert fake.calls == []


# ----------------------------------------------- robustness (verifier notes) ---

def test_a_drifted_decisions_package_keeps_the_regex_hints(hook, tmp_path, monkeypatch) -> None:
    """Hook and package out of step (``build_hints`` missing or failing) must not cost the regex hints."""
    from memorymaster.decisions import questions

    fake = FakeTransport({"preference": 0.99})
    monkeypatch.delattr(questions, "build_hints")
    assert hook.run(json.loads(DECIDED["stdin"]), engine=make_engine(tmp_path, fake)) == DECIDED["stdout"]

    def drifted(_prompt):
        raise TypeError("drifted signature")

    monkeypatch.setattr(questions, "build_hints", drifted, raising=False)
    assert hook.run(json.loads(DECIDED["stdin"]), engine=make_engine(tmp_path, fake)) == DECIDED["stdout"]
    assert fake.calls == []
    assert [kw.get("jev") for event, kw in hook.events if event == "matched"] == ["error", "error"]


def test_a_write_locked_ledger_cannot_push_the_hook_past_its_timeout(hook, tmp_path, monkeypatch) -> None:
    """Verifier probe_hook_locked: with another writer holding decisions.db the hook ran ~16.7 s (the
    ledger's 15 s busy wait), past the installed 5 s hook timeout, so even the regex hints were lost."""
    from memorymaster.decisions import transport as transport_module

    fake = FakeTransport({"preference": 0.99})
    monkeypatch.setattr(transport_module, "HttpxTransport", lambda key, **kw: fake)  # never the network
    db = tmp_path / "decisions.db"
    monkeypatch.setenv("MEMORYMASTER_JEV_HINTS", "live")
    monkeypatch.setenv("MEMORYMASTER_DECISIONS_DB", str(db))
    monkeypatch.setenv("TYPESAFE_API_KEY", KEY.reveal())

    assert hint_names(hook.run(json.loads(DECIDED["stdin"]))) == ["PREFERENCE"]  # creates the ledger
    lock = sqlite3.connect(str(db), timeout=1)
    try:
        lock.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        out = hook.run(json.loads(DECIDED["stdin"]))
        elapsed = time.monotonic() - started
    finally:
        lock.rollback()
        lock.close()

    assert elapsed < 3.0, elapsed
    assert out == DECIDED["stdout"]  # an unreadable ledger never acts on Jev: the regex hints survive


def test_session_key_is_the_shared_normalized_hash_never_the_raw_session_id(hook, tmp_path) -> None:
    from memorymaster.recall.jev_surfaces import decision_session_key

    engine = make_engine(tmp_path, FakeTransport({"decision": 0.9}))
    for raw in ("Sess-RAW-42", "  sess-raw-42 ", "C:/Users/x/.claude/projects/p/Sess-RAW-42.jsonl"):
        hook.run({"prompt": "We decided to use WAL mode for the ledger.", "session_id": raw}, engine=engine)

    keys = {row["session_key"] for row in rows(tmp_path, "SELECT session_key FROM decisions")}
    assert keys == {decision_session_key("Sess-RAW-42")}  # the prompt and Stop hooks use this key
    stored = b"".join(path.read_bytes() for path in sorted(tmp_path.glob("decisions.db*")))
    assert b"sess-raw-42" not in stored.lower()


"""S5 SKILLS: Jev skill suggestion on the shared decision engine (no subprocess worker).

Every request goes to a LOCAL http.server through the real in-process
``HttpxTransport`` (never the provider).  Covers the two-round design and its
validation rules, the cookbook procedure gate, logging of every decision
(surface ``skills``) including requests withheld before the engine (verifier
S5-UNLOGGED-FALLBACKS), the legacy ``MEMORYMASTER_JEV_SKILLS_ENABLED`` flag, and,
through the real ``recall_skills`` path, review F-15 (a planted ``requests.py`` /
``json.py`` in the working directory must never see the key) and the review-A
r5 egress cases (private IPs and emails leave only redacted; home paths and
passwords, which the ingest scanner flags, never leave).
"""
from __future__ import annotations

import http.server
import json
import math
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

import pytest

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.decisions import transport as transport_module
from memorymaster.decisions.config import DecisionConfig
from memorymaster.decisions.credentials import ApiKey
from memorymaster.decisions.engine import DecisionEngine
from memorymaster.decisions.ledger import DecisionLedger
from memorymaster.decisions.transport import HttpxTransport
from memorymaster.knowledge import jev_selector
from memorymaster.knowledge.jev_selector import select_skill_ids, skills_config
from memorymaster.knowledge.skills import approve_skill_candidate, build_skill_fields, recall_skills

SYNTHETIC_KEY = "ts-test-" + "S" * 24  # synthetic, never a real credential
KEY = ApiKey(SYNTHETIC_KEY)
GATE = "skills.needs_procedure"
WHICH = "skills.which_skill"


# ------------------------------------------------------------ local server ---

class _QuietServer(http.server.ThreadingHTTPServer):
    def handle_error(self, request, client_address):  # client aborts are expected here
        pass


def noul(value: float) -> dict[str, Any]:
    return {"type": "noul", "noul": value}


def choice(pick: str, probabilities: dict[str, float], confidence: float | None = 0.9) -> dict[str, Any]:
    answer: dict[str, Any] = {"type": "choice", "choice": pick, "probabilities": probabilities}
    if confidence is not None:
        answer["confidence"] = confidence
    return answer


def body(answers: dict[str, Any]) -> dict[str, Any]:
    return {"model": "jev-1.13.0", "answers": answers, "usage": {"input_tokens": 400, "output_tokens": 20}}


def default_answers(payload: dict[str, Any]) -> dict[str, Any]:
    """Valid answers for whatever was asked: gate open, confident 'none', every fit high."""
    answers: dict[str, Any] = {}
    for wire_id, question in payload["questions"].items():
        if question["type"] == "noul":
            answers[wire_id] = noul(0.9)
        else:
            options = list(question["criteria"])
            answers[wire_id] = choice("none", {o: (1.0 if o == "none" else 0.0) for o in options})
    return answers


class JevStub:
    """Local System One stand-in.  ``script`` entries are ``callable(payload) -> (status, body)``."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.script: list[Callable[[dict[str, Any]], tuple[int, Any]]] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):  # noqa: N802
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                payload = json.loads(raw)
                outer.requests.append({"headers": dict(self.headers), "raw": raw, "payload": payload})
                action = outer.script.pop(0) if outer.script else (lambda p: (200, body(default_answers(p))))
                status, response = action(payload)
                data = json.dumps(response).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass

            def log_message(self, *args):
                pass

        self.httpd = _QuietServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/v1/systemone"

    def reply(self, answers: dict[str, Any], status: int = 200, delay: float = 0.0) -> None:
        def action(_payload):
            if delay:
                time.sleep(delay)
            return status, body(answers)
        self.script.append(action)

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture()
def stub():
    server = JevStub()
    yield server
    server.close()


def make_engine(tmp_path: Path, stub: JevStub, *, mode: str = "live", key: ApiKey | None = KEY,
                env: dict[str, str] | None = None) -> DecisionEngine:
    environ = {"MEMORYMASTER_JEV_MODE": mode, "MEMORYMASTER_DECISIONS_DB": str(tmp_path / "decisions.db")}
    environ.update(env or {})
    return DecisionEngine(DecisionConfig.from_env(environ),
                          transport_factory=lambda k: HttpxTransport(k, endpoint=stub.url),
                          key_lookup=lambda: key)


def ledger_rows(tmp_path: Path, sql: str, params=()) -> list[dict[str, Any]]:
    return DecisionLedger(tmp_path / "decisions.db").query(sql, params)


def skills(count: int = 2) -> list[dict[str, Any]]:
    return [
        {
            "claim_id": index,
            "title": f"Skill {index}",
            "when_to_use": f"When request {index} applies.",
            "when_not_to_use": "When the request is unrelated.",
            "workflow": ["Inspect the bounded request.", "Run the focused check."],
            "decision_rules": ["Stop if authorization is missing."],
            "validation": ["Confirm the focused check."],
            "citations": [{"source": "must-never-leave"}],
            "scope": "must-never-leave",
        }
        for index in range(1, count + 1)
    ]


WIDE_OK = {GATE: noul(0.9), WHICH: choice("skill:1", {"none": 0.05, "skill:1": 0.8, "skill:2": 0.15})}
DETAILED_OK = {
    WHICH: choice("skill:1", {"none": 0.01, "skill:1": 0.95, "skill:2": 0.04}),
    "skills.fits::claim:1": noul(0.8),
    "skills.fits::claim:2": noul(0.99),
}


# ------------------------------------------------------------- two rounds ---

def test_two_rounds_pick_one_skill_and_log_both_decisions(tmp_path, stub) -> None:
    stub.reply(WIDE_OK)
    stub.reply(DETAILED_OK)
    engine = make_engine(tmp_path, stub)

    assert select_skill_ids("Run request 1", skills(), legacy_ids=[2], engine=engine, scope="project:t") == [1]

    assert len(stub.requests) == 2
    first, second = (r["payload"] for r in stub.requests)
    assert stub.requests[0]["headers"]["Authorization"] == f"Bearer {SYNTHETIC_KEY}"
    assert first["model"] == "jev-1.13.0"
    assert set(first["questions"]) == {GATE, WHICH}
    wide = first["questions"][WHICH]["criteria"]
    assert set(wide) == {"none", "skill:1", "skill:2"}
    assert set(wide["skill:1"]) == {"title", "when_to_use", "when_not_to_use"}
    assert set(second["questions"]) == {WHICH, "skills.fits::claim:1", "skills.fits::claim:2"}
    detailed = second["questions"][WHICH]["criteria"]
    assert set(detailed["skill:1"]) == {"title", "when_to_use", "when_not_to_use", "workflow", "decision_rules",
                                        "validation"}
    for request in stub.requests:
        assert b"must-never-leave" not in request["raw"] and b"citations" not in request["raw"]

    decisions = ledger_rows(tmp_path, "SELECT * FROM decisions ORDER BY ts, rowid")
    assert [d["surface"] for d in decisions] == ["skills", "skills"]
    assert all(d["mode"] == "live" and d["fallback_reason"] is None for d in decisions)
    wide_row, detailed_row = decisions
    assert json.loads(wide_row["baseline_features_json"])["round"] == "wide"
    features = json.loads(detailed_row["baseline_features_json"])
    assert features["round"] == "detailed" and features["wide_decision_id"] == wide_row["decision_id"]
    assert json.loads(detailed_row["action_taken"]) == ["claim:1"]
    assert json.loads(wide_row["legacy_action"]) == ["claim:2"]
    exposed = ledger_rows(tmp_path, "SELECT decision_id, item_ref FROM decision_items WHERE exposed = 1")
    assert exposed == [{"decision_id": detailed_row["decision_id"], "item_ref": "claim:1"}]


def test_closed_procedure_gate_is_a_confident_no_skill_in_one_request(tmp_path, stub) -> None:
    stub.reply({GATE: noul(0.2), WHICH: choice("skill:1", {"none": 0.05, "skill:1": 0.9, "skill:2": 0.05})})

    assert select_skill_ids("Tell me a joke", skills(), legacy_ids=[1], engine=make_engine(tmp_path, stub)) == []
    assert len(stub.requests) == 1
    row = ledger_rows(tmp_path, "SELECT action_taken, fallback_reason FROM decisions")[0]
    assert json.loads(row["action_taken"]) == [] and row["fallback_reason"] is None


def test_confident_none_is_distinct_from_fallback(tmp_path, stub) -> None:
    stub.reply({GATE: noul(0.9), WHICH: choice("none", {"none": 0.9, "skill:1": 0.05, "skill:2": 0.05})})

    assert select_skill_ids("Unrelated prose", skills(), legacy_ids=[1], engine=make_engine(tmp_path, stub)) == []
    assert len(stub.requests) == 1


def test_low_confidence_or_low_winner_fit_keep_the_legacy_selection(tmp_path, stub) -> None:
    stub.reply({GATE: noul(0.9), WHICH: choice("skill:1", {"none": 0.1, "skill:1": 0.8, "skill:2": 0.1}, 0.2)})
    engine = make_engine(tmp_path, stub)
    assert select_skill_ids("Run request 1", skills(), legacy_ids=[2], engine=engine) is None
    assert len(stub.requests) == 1

    stub.reply(WIDE_OK)
    stub.reply({**DETAILED_OK, "skills.fits::claim:1": noul(0.2)})  # a high fit elsewhere cannot rescue the winner
    assert select_skill_ids("Run request 1", skills(), legacy_ids=[2], engine=engine) is None

    stub.reply({GATE: noul(0.9), WHICH: choice("skill:1", {"none": 0.1, "skill:1": 0.8, "skill:2": 0.1}, None)})
    assert select_skill_ids("Run request 1", skills(), legacy_ids=[2], engine=engine) is None

    rows = ledger_rows(tmp_path, "SELECT fallback_reason, action_taken, jev_action FROM decisions ORDER BY ts, rowid")
    finals = [rows[0], rows[2], rows[3]]
    assert all(r["fallback_reason"] is None for r in finals)
    assert all(json.loads(r["action_taken"]) == ["claim:2"] == json.loads(r["jev_action"]) for r in finals)


def test_contradictory_unknown_or_nonfinite_answers_fall_back_and_are_logged(tmp_path, stub) -> None:
    engine = make_engine(tmp_path, stub)
    stub.reply({GATE: noul(0.9), WHICH: choice("none", {"none": 0.01, "skill:1": 0.98, "skill:2": 0.01})})
    assert select_skill_ids("Run request 1", skills(), legacy_ids=[1], engine=engine) is None
    stub.reply(WIDE_OK)
    stub.reply({**DETAILED_OK, WHICH: choice("none", {"none": 0.01, "skill:1": 0.95, "skill:2": 0.04})})
    assert select_skill_ids("Run request 1", skills(), legacy_ids=[1], engine=engine) is None
    stub.reply({GATE: noul(0.9), WHICH: choice("skill:999", {"none": 0.1, "skill:1": 0.8, "skill:999": 0.1})})
    assert select_skill_ids("Run request 1", skills(), legacy_ids=[1], engine=engine) is None
    stub.reply({GATE: noul(0.9), WHICH: choice("skill:1", {"none": 0.1, "skill:1": math.nan, "skill:2": 0.1})})
    assert select_skill_ids("Run request 1", skills(), legacy_ids=[1], engine=engine) is None
    stub.reply({WHICH: WIDE_OK[WHICH]})  # the gate went unanswered
    assert select_skill_ids("Run request 1", skills(), legacy_ids=[1], engine=engine) is None

    reasons = [r["fallback_reason"] for r in ledger_rows(tmp_path, "SELECT fallback_reason FROM decisions "
                                                                    "ORDER BY ts, rowid")]
    assert reasons == ["choose_error", None, "choose_error", "malformed", "malformed", "malformed"]


def test_http_errors_and_timeouts_degrade_to_legacy_within_the_deadline(tmp_path, stub) -> None:
    engine = make_engine(tmp_path, stub)
    stub.reply({}, status=503)
    assert select_skill_ids("Run request 1", skills(), legacy_ids=[1], engine=engine) is None

    stub.reply(WIDE_OK, delay=2.0)
    started = time.monotonic()
    assert select_skill_ids("Run request 1", skills(), legacy_ids=[1], engine=engine) is None
    assert time.monotonic() - started < 1.5  # hook deadline 900 ms, legacy returned at once
    reasons = [r["fallback_reason"] for r in ledger_rows(tmp_path, "SELECT fallback_reason FROM decisions "
                                                                    "ORDER BY ts, rowid")]
    assert reasons == ["http_5xx", "timeout"]


def test_off_mode_and_missing_key_send_nothing(tmp_path, stub) -> None:
    assert select_skill_ids("Run request 1", skills(), engine=make_engine(tmp_path, stub, mode="off")) is None
    assert not (tmp_path / "decisions.db").exists()
    assert select_skill_ids("Run request 1", skills(), engine=make_engine(tmp_path, stub, key=None)) is None
    assert stub.requests == []
    assert [r["fallback_reason"] for r in ledger_rows(tmp_path, "SELECT fallback_reason FROM decisions")] == [
        "missing_key"]


def test_shadow_mode_keeps_legacy_but_logs_what_jev_would_suggest(tmp_path, stub) -> None:
    stub.reply(WIDE_OK)
    stub.reply(DETAILED_OK)

    assert select_skill_ids("Run request 1", skills(), legacy_ids=[2],
                            engine=make_engine(tmp_path, stub, mode="shadow")) is None
    assert len(stub.requests) == 2
    wide_row, detailed_row = ledger_rows(tmp_path, "SELECT * FROM decisions ORDER BY ts, rowid")
    assert wide_row["mode"] == detailed_row["mode"] == "shadow"
    assert json.loads(detailed_row["jev_action"]) == ["claim:1"]
    assert json.loads(detailed_row["action_taken"]) == ["claim:2"]
    exposed = ledger_rows(tmp_path, "SELECT decision_id, item_ref FROM decision_items WHERE exposed = 1")
    assert exposed == [{"decision_id": wide_row["decision_id"], "item_ref": "claim:2"}]  # counted once


def ledger_bytes(tmp_path: Path) -> bytes:
    """Every byte of the ledger, WAL included (rows may not be checkpointed yet)."""
    return b"".join(path.read_bytes() for path in sorted(tmp_path.glob("decisions.db*")))


def _with(field: str, value: Any, index: int = 1, count: int = 2) -> list[dict[str, Any]]:
    catalog = skills(count)
    catalog[index][field] = value
    return catalog


def _oversized_payload() -> list[dict[str, Any]]:
    catalog = skills(200)
    for skill in catalog:
        skill["when_to_use"] = "Use it when the bounded request applies. " * 7  # < 320 chars each, > 64 KiB total
    return catalog


# (query, catalog) -> (fallback_reason, withheld label, withheld ref)
PRE_ENGINE_FALLBACKS = {
    "catalog_too_large": (("Run request 1", lambda: skills(201)), ("request_too_large", None)),
    "query_too_long": (("x" * 4_001, skills), ("request_too_large", None)),
    "payload_too_large": (("Run request 1", _oversized_payload), ("request_too_large", None)),
    "descriptor_path": (("Run request 1", lambda: _with("workflow", ["Read C:\\private\\runbook.md"])),
                        ("egress_blocked", "claim:2")),
    "descriptor_invalid": (("Run request 1", lambda: _with("when_not_to_use", "")), ("invalid_request", "claim:2")),
    "query_empty": (("   ", skills), ("invalid_request", None)),
    "catalog_empty": (("Run request 1", list), ("invalid_request", None)),
}


@pytest.mark.parametrize("label", list(PRE_ENGINE_FALLBACKS))
def test_invalid_catalog_or_query_falls_back_without_a_request_but_is_logged(label, tmp_path, stub) -> None:
    """Verifier S5-UNLOGGED-FALLBACKS: a fallback decided before the engine still writes one row, with no text."""
    (query, catalog), (reason, ref) = PRE_ENGINE_FALLBACKS[label]
    engine = make_engine(tmp_path, stub)

    assert select_skill_ids(query, catalog(), legacy_ids=[1], engine=engine, scope="project:t") is None

    assert stub.requests == []
    (row,) = ledger_rows(tmp_path, "SELECT * FROM decisions")
    assert (row["surface"], row["mode"], row["fallback_reason"]) == ("skills", "live", reason)
    assert (row["transport_outcome"], row["attempt_count"], row["cost_usd"]) == ("not_sent", 0, 0.0)
    assert row["state_redacted"] is None and row["state_sha256"] is None and row["egress_bytes"] is None
    assert json.loads(row["legacy_action"]) == json.loads(row["action_taken"]) == ["claim:1"]
    assert row["exploration_arm"] == "fallback" and row["chosen_propensity"] == 1.0
    assert row["scope"] == "project:t" and row["engine_ms"] is not None
    features = json.loads(row["baseline_features_json"])
    assert features["withheld"] == label and features.get("withheld_ref") == ref
    items = ledger_rows(tmp_path, "SELECT item_ref, exposed, delivered FROM decision_items")
    assert items == [{"item_ref": "claim:1", "exposed": 1, "delivered": 1}]
    stored = ledger_bytes(tmp_path)
    assert b"xxxxxxxxxxxxxxxx" not in stored and b"runbook" not in stored and b"bounded request" not in stored


def test_withheld_rows_follow_the_mode_and_never_trip_the_breaker(tmp_path, stub) -> None:
    off = make_engine(tmp_path, stub, mode="off")
    assert select_skill_ids("x" * 4_001, skills(), legacy_ids=[1], engine=off) is None
    assert not (tmp_path / "decisions.db").exists()  # off writes nothing, like the engine

    logged_off = make_engine(tmp_path, stub, mode="off", env={"MEMORYMASTER_DECISIONS_LOG_OFF": "1"})
    assert select_skill_ids("x" * 4_001, skills(), legacy_ids=[1], engine=logged_off) is None
    assert [(r["mode"], r["fallback_reason"]) for r in ledger_rows(tmp_path, "SELECT * FROM decisions")] == [
        ("off", "mode_off")]

    shadow = make_engine(tmp_path, stub, mode="shadow")
    assert select_skill_ids("x" * 4_001, skills(), legacy_ids=[1], engine=shadow) is None
    live = make_engine(tmp_path, stub)
    for _ in range(12):  # nothing was sent: neither the breaker nor the RPM budget counts these rows
        assert select_skill_ids("x" * 4_001, skills(), legacy_ids=[1], engine=live) is None
    stub.reply(WIDE_OK)
    stub.reply(DETAILED_OK)
    assert select_skill_ids("Run request 1", skills(), legacy_ids=[2], engine=live) == [1]
    assert stub.requests and len(stub.requests) == 2
    rows = ledger_rows(tmp_path, "SELECT mode, fallback_reason FROM decisions ORDER BY ts, rowid")
    assert rows[1] == {"mode": "shadow", "fallback_reason": "request_too_large"}
    assert rows[-2:] == [{"mode": "live", "fallback_reason": None}] * 2


def test_public_urls_in_descriptors_are_not_paths(tmp_path, stub) -> None:
    """Verifier: ``s:/`` in ``https://`` used to withhold the whole catalog (the drive rule's false positive)."""
    catalog = _with("workflow", ["Check the docs at https://docs.example.com/guide first."])
    catalog[0]["validation"] = ["Compare with git+ssh://example.com/team/repo.git."]

    assert select_skill_ids("Run request 1", catalog, legacy_ids=[1], engine=make_engine(tmp_path, stub)) == []
    assert len(stub.requests) == 1
    for path in ("Read G:/builds/run.ps1", "Open file:///home/build/run.sh", "Run /opt/build/run.sh",
                 "Copy \\\\server\\share\\run.cmd", "Open file:///C:/builds/run.ps1"):
        assert jev_selector._has_path(path), path
    for text in ("See https://docs.example.com/guide", "Use git+ssh://example.com/team/repo.git",
                 "Ratio 3:1 is fine", "Tests: /status then retry"):
        assert not jev_selector._has_path(text), text


# Verifier S5-URL-HOME-PATH-EGRESS: a home (or system) directory inside a URL is still a
# local path.  The egress redactor leaves it in clear, so such a descriptor must never leave.
URL_HOME_STEPS = (
    "Copy the report to smb://nas/home/jdoe/docs when done.",
    "Upload with sftp://build/Users/jdoe/releases when done.",
    "Publish to https://nas.lan/home/jdoe/site when done.",
)


def test_home_and_system_dirs_inside_urls_are_paths() -> None:
    for text in (*URL_HOME_STEPS, "Read https://docs.example.com/users/jdoe/guide first.",
                 "Use git+ssh://example.com/etc/repo.git", "Compare with http://example.org/tmp/report."):
        assert jev_selector._has_path(text), text


def test_legacy_enable_flag_maps_onto_the_skills_mode() -> None:
    assert skills_config({}).mode_for("skills") == "off"
    assert skills_config({"MEMORYMASTER_JEV_SKILLS_ENABLED": "1"}).mode_for("skills") == "live"
    assert skills_config({"MEMORYMASTER_JEV_SKILLS_ENABLED": "true"}).mode_for("skills") == "live"
    assert skills_config({"MEMORYMASTER_JEV_SKILLS_ENABLED": "0"}).mode_for("skills") == "off"
    # The new per-surface and global modes win over the legacy flag.
    assert skills_config({"MEMORYMASTER_JEV_SKILLS_ENABLED": "1",
                          "MEMORYMASTER_JEV_SKILLS": "shadow"}).mode_for("skills") == "shadow"
    assert skills_config({"MEMORYMASTER_JEV_SKILLS_ENABLED": "1",
                          "MEMORYMASTER_JEV_SKILLS": "off"}).mode_for("skills") == "off"
    assert skills_config({"MEMORYMASTER_JEV_SKILLS_ENABLED": "0",
                          "MEMORYMASTER_JEV_MODE": "live"}).mode_for("skills") == "live"
    # The documented kill switch wins over the old flag (verifier: it used to stay live).
    assert skills_config({"MEMORYMASTER_JEV_SKILLS_ENABLED": "1",
                          "MEMORYMASTER_JEV_MODE": "off"}).mode_for("skills") == "off"
    assert skills_config({"MEMORYMASTER_JEV_SKILLS_ENABLED": "1",
                          "MEMORYMASTER_JEV_MODE": "shadow"}).mode_for("skills") == "shadow"
    assert skills_config({"MEMORYMASTER_JEV_SKILLS_ENABLED": "1",
                          "MEMORYMASTER_JEV_MODE": "garbage"}).mode_for("skills") == "off"
    assert skills_config({"MEMORYMASTER_JEV_SKILLS_ENABLED": "1",
                          "MEMORYMASTER_JEV_MODE": "  "}).mode_for("skills") == "live"
    # The flag only concerns skills.
    assert skills_config({"MEMORYMASTER_JEV_SKILLS_ENABLED": "1"}).mode_for("recall") == "off"


# ------------------------------------------------ the real recall_skills path ---

def _skill_payload(slug: str, title: str, when: str) -> dict[str, Any]:
    return {
        "schema": "personal-skill-v1", "slug": slug, "title": title,
        "when_to_use": when, "when_not_to_use": "For unrelated work.",
        "inputs": ["candidate"], "prerequisites": ["disposable database"],
        "workflow": ["Run the focused tests."], "decision_rules": ["Stop on failure."],
        "expected_output": "Evidence", "validation": ["Check test results."],
        "pitfalls": ["Do not ignore failures."], "recovery": ["Restore the candidate."],
        "quality_scores": dict.fromkeys(["recurrence", "reusability", "executability", "validation", "safety"], 16),
    }


@pytest.fixture()
def skill_service(tmp_path: Path):
    service = MemoryService(tmp_path / "skills.db", workspace_root=tmp_path)
    service.init_db()
    ids = []
    for slug, title, when in (("release-check", "Release check", "Before preparing a release."),
                              ("deploy-check", "Deploy check", "Before deploying a service.")):
        claim = service.ingest(**build_skill_fields(_skill_payload(slug, title, when), supporting_claim_ids=[1]),
                               citations=[CitationInput(source="test", locator="fixture")],
                               scope="project:test", source_agent="fixture")
        approve_skill_candidate(service, claim.id, actor="test-operator")
        ids.append(claim.id)
    service.skill_ids = ids
    return service


@pytest.fixture()
def live_skills_env(tmp_path, stub, monkeypatch):
    """Legacy flag on, synthetic key, temp ledger, and the pinned transport redirected to the local stub."""
    real = transport_module.HttpxTransport
    monkeypatch.setattr(transport_module, "HttpxTransport", lambda key, **kw: real(key, endpoint=stub.url))
    monkeypatch.setenv("MEMORYMASTER_JEV_SKILLS_ENABLED", "1")
    monkeypatch.setenv("TYPESAFE_API_KEY", SYNTHETIC_KEY)
    monkeypatch.setenv("MEMORYMASTER_DECISIONS_DB", str(tmp_path / "decisions.db"))
    return stub


def test_f15_planted_modules_in_cwd_never_see_the_key(tmp_path, skill_service, live_skills_env, monkeypatch) -> None:
    """Review F-15 / review-A r4: the selector must not run a child interpreter from the cwd."""
    evil = tmp_path / "evilcwd"
    evil.mkdir()
    for name in ("requests", "json"):
        (evil / f"{name}.py").write_text(
            "import sys\n"
            "open(__file__ + '.captured', 'w').write(sys.stdin.read())\n"
            "raise SystemExit(1)\n",
            encoding="utf-8",
        )
    spawned: list[Any] = []
    real_popen = subprocess.Popen

    def spy(*args, **kwargs):
        spawned.append(args[0] if args else kwargs.get("args"))
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", spy)
    monkeypatch.chdir(evil)

    recall_skills(skill_service, "release check", scope_allowlist=["project:test"])

    assert sorted(p.name for p in evil.glob("*.captured")) == []
    assert spawned == []
    assert [r["headers"]["Authorization"] for r in live_skills_env.requests][:1] == [f"Bearer {SYNTHETIC_KEY}"]


# query -> (values that must never leave, whether the scanner already withholds the whole query)
R5_QUERIES = {
    "release for C:\\Users\\jdoe\\clients\\acme-secret\\build.ps1": (["jdoe"], True),
    "release to 10.20.30.40 and 192.168.1.10 via wg0": (["10.20.30.40", "192.168.1.10"], False),
    "release notes for jane.doe@clientcorp.com": (["jane.doe@clientcorp.com", "clientcorp"], False),
    "release with password=hunter2": (["hunter2"], True),
}


@pytest.mark.parametrize("query", list(R5_QUERIES))
def test_r5_queries_leave_the_machine_only_redacted(query, tmp_path, skill_service, live_skills_env,
                                                     monkeypatch) -> None:
    """Review-A r5: the raw query used to be sent whenever the ingest scanner found nothing."""
    child_inputs: list[str] = []

    def no_child(*args, **kwargs):  # the pre-4.9 worker; never reach the network from a test
        child_inputs.append(str(kwargs.get("input") or ""))
        raise subprocess.CalledProcessError(1, "blocked-in-test")

    monkeypatch.setattr(subprocess, "run", no_child)
    private_values, withheld = R5_QUERIES[query]

    recall_skills(skill_service, query, scope_allowlist=["project:test"])

    sent = [r["raw"].decode("utf-8") for r in live_skills_env.requests] + child_inputs
    for private in private_values:
        assert all(private not in text for text in sent), private
    if withheld:  # a scanner finding keeps the whole query in the process (pre-4.9 guard)
        assert sent == []
        return
    assert sent, "a clean-looking query is decided (and logged) through the engine"
    row = ledger_rows(tmp_path, "SELECT surface, redaction_counts_json FROM decisions ORDER BY ts, rowid")[0]
    assert row["surface"] == "skills" and json.loads(row["redaction_counts_json"])


def test_jev_pick_is_reauthorized_against_sqlite(tmp_path, skill_service, live_skills_env) -> None:
    release_id, deploy_id = skill_service.skill_ids
    wide = {GATE: noul(0.9), WHICH: choice(f"skill:{deploy_id}", {"none": 0.05, f"skill:{release_id}": 0.15,
                                                                   f"skill:{deploy_id}": 0.8})}
    detailed = {WHICH: choice(f"skill:{deploy_id}", {"none": 0.05, f"skill:{release_id}": 0.05,
                                                     f"skill:{deploy_id}": 0.9}),
                f"skills.fits::claim:{release_id}": noul(0.3), f"skills.fits::claim:{deploy_id}": noul(0.9)}
    live_skills_env.reply(wide)
    live_skills_env.reply(detailed)

    hits = recall_skills(skill_service, "release check", scope_allowlist=["project:test"])
    assert [h["claim_id"] for h in hits] == [deploy_id]  # Jev's pick, rehydrated through SQLite

    def retire_then_answer(_payload):
        with sqlite3.connect(skill_service.store.db_path) as conn:  # disposable fixture DB only
            conn.execute("UPDATE claims SET status = 'archived' WHERE id = ?", (deploy_id,))
        return 200, body(detailed)

    live_skills_env.reply(wide)
    live_skills_env.script.append(retire_then_answer)
    hits = recall_skills(skill_service, "release check", scope_allowlist=["project:test"])
    assert [h["claim_id"] for h in hits] == [release_id]  # a retired pick never revives: lexical fallback

    # The ledger records what was actually delivered (verifier: a retired pick was logged as delivered).
    picks = ledger_rows(tmp_path, "SELECT i.decision_id, i.exposed, i.delivered FROM decision_items i "
                                  "JOIN decisions d USING (decision_id) WHERE i.item_ref = ? AND i.question_id = 'skills.fits' "
                                  "AND d.baseline_features_json LIKE '%detailed%' ORDER BY d.ts, d.rowid",
                        (f"claim:{deploy_id}",))
    assert [(p["exposed"], p["delivered"]) for p in picks] == [(1, 1), (1, 0)]


def _withheld_row(tmp_path: Path) -> dict[str, Any]:
    (row,) = ledger_rows(tmp_path, "SELECT * FROM decisions")
    assert (row["surface"], row["mode"], row["transport_outcome"], row["attempt_count"]) == (
        "skills", "live", "not_sent", 0)
    assert row["state_redacted"] is None and row["state_sha256"] is None
    return row


def test_private_catalog_entries_never_reach_the_selector(tmp_path, skill_service, live_skills_env) -> None:
    private_id = skill_service.skill_ids[1]
    with sqlite3.connect(skill_service.store.db_path) as conn:  # disposable fixture DB only
        conn.execute("UPDATE claims SET visibility = 'private' WHERE id = ?", (private_id,))

    hits = recall_skills(skill_service, "release check", scope_allowlist=["project:test"])
    assert live_skills_env.requests == []
    row = _withheld_row(tmp_path)  # verifier S5-UNLOGGED-FALLBACKS: withheld, but logged
    assert row["fallback_reason"] == "egress_blocked"
    features = json.loads(row["baseline_features_json"])
    assert features == {"round": "wide", "catalog_size": 2, "withheld": "catalog_private",
                        "withheld_ref": f"claim:{private_id}"}
    assert json.loads(row["action_taken"]) == [f"claim:{h['claim_id']}" for h in hits]
    assert b"release check" not in ledger_bytes(tmp_path) and b"Release check" not in ledger_bytes(tmp_path)


# query -> (fallback_reason, withheld label); the scanner / size guards run before the engine.
WITHHELD_QUERIES = {
    "release with password=hunter2": ("egress_blocked", "query_sensitive"),
    "release [REDACTED:secret] check": ("egress_blocked", "query_sensitive"),
    "release check " + "x" * 3_990: ("request_too_large", "query_too_long"),
}


@pytest.mark.parametrize("query", list(WITHHELD_QUERIES), ids=["password", "redaction_marker", "too_long"])
def test_withheld_queries_are_logged_through_the_real_path(query, tmp_path, skill_service, live_skills_env) -> None:
    """Verifier probe_s5_prelog: these used to fall back with zero ledger rows."""
    reason, label = WITHHELD_QUERIES[query]
    release_id = skill_service.skill_ids[0]

    hits = recall_skills(skill_service, query, scope_allowlist=["project:test"])

    assert hits[0]["claim_id"] == release_id  # the lexical selection is unchanged
    assert live_skills_env.requests == []
    row = _withheld_row(tmp_path)
    assert row["fallback_reason"] == reason
    assert json.loads(row["baseline_features_json"])["withheld"] == label
    delivered = [f"claim:{h['claim_id']}" for h in hits]
    assert json.loads(row["legacy_action"]) == json.loads(row["action_taken"]) == delivered
    stored = ledger_bytes(tmp_path)
    assert b"hunter2" not in stored and b"release" not in stored and b"xxxxxxxx" not in stored


def _add_skill(service: MemoryService, slug: str, workflow: list[str]) -> int:
    payload = _skill_payload(slug, f"Build {slug}", f"Before running the {slug} build.")  # distinct: no dedup
    payload["workflow"] = workflow
    claim = service.ingest(**build_skill_fields(payload, supporting_claim_ids=[1]),
                           citations=[CitationInput(source="test", locator="fixture")],
                           scope="project:test", source_agent="fixture")
    approve_skill_candidate(service, claim.id, actor="test-operator")
    return claim.id


def test_path_bearing_skill_is_logged_and_public_urls_still_reach_jev(tmp_path, skill_service,
                                                                       live_skills_env) -> None:
    url_id = _add_skill(skill_service, "url-check", ["Read https://docs.example.com/guide first."])
    recall_skills(skill_service, "release check", scope_allowlist=["project:test"])
    assert len(live_skills_env.requests) == 1  # a public URL is not a path: the catalog is decided
    assert f"skill:{url_id}" in live_skills_env.requests[0]["payload"]["questions"][WHICH]["criteria"]

    path_id = _add_skill(skill_service, "path-check", ["Run /opt/build/run.sh"])
    assert len({*skill_service.skill_ids, url_id, path_id}) == 4
    hits = recall_skills(skill_service, "release check", scope_allowlist=["project:test"])
    assert len(live_skills_env.requests) == 1  # withheld: nothing more was sent
    assert hits and hits[0]["claim_id"] == skill_service.skill_ids[0]
    rows = ledger_rows(tmp_path, "SELECT * FROM decisions ORDER BY ts, rowid")
    assert len(rows) == 2 and rows[-1]["fallback_reason"] == "egress_blocked"
    assert json.loads(rows[-1]["baseline_features_json"])["withheld_ref"] == f"claim:{path_id}"
    assert b"/opt/build" not in ledger_bytes(tmp_path)


def _pick(option: str) -> Callable[[dict[str, Any]], tuple[int, Any]]:
    """A stub answer that opens the gate and confidently picks ``option`` with every fit high."""
    def action(payload: dict[str, Any]) -> tuple[int, Any]:
        answers: dict[str, Any] = {}
        for wire_id, question in payload["questions"].items():
            if question["type"] == "noul":
                answers[wire_id] = noul(0.9)
            else:
                answers[wire_id] = choice(option, {o: (1.0 if o == option else 0.0) for o in question["criteria"]},
                                          0.95)
        return 200, body(answers)
    return action


@pytest.mark.parametrize("step", URL_HOME_STEPS, ids=["smb", "sftp", "https"])
def test_home_dir_inside_a_url_never_leaves_through_the_real_path(step, tmp_path, skill_service,
                                                                   live_skills_env) -> None:
    """Verifier probe_smb: the detailed round sent the workflow step with the username in clear."""
    home_id = _add_skill(skill_service, "copy-check", ["Run the release tests.", step])
    live_skills_env.script.extend([_pick(f"skill:{home_id}"), _pick(f"skill:{home_id}")])

    hits = recall_skills(skill_service, "release check", scope_allowlist=["project:test"])

    assert all(b"jdoe" not in r["raw"] for r in live_skills_env.requests)
    assert live_skills_env.requests == []  # one such skill keeps the whole catalog local
    assert hits and hits[0]["claim_id"] == skill_service.skill_ids[0]  # lexical selection
    row = _withheld_row(tmp_path)
    assert row["fallback_reason"] == "egress_blocked"
    features = json.loads(row["baseline_features_json"])
    assert (features["withheld"], features["withheld_ref"]) == ("descriptor_path", f"claim:{home_id}")
    assert b"jdoe" not in ledger_bytes(tmp_path)


def test_selector_module_has_no_child_process_worker() -> None:
    assert not hasattr(jev_selector, "subprocess")
    assert not hasattr(jev_selector, "_HTTP_WORKER")


def test_session_key_is_logged_normalized_and_tenant_bound_never_raw(tmp_path, stub) -> None:
    from memorymaster.recall.jev_surfaces import decision_session_key

    stub.reply(WIDE_OK)
    stub.reply(DETAILED_OK)
    engine = make_engine(tmp_path, stub)

    assert select_skill_ids("Run request 1", skills(), legacy_ids=[2], engine=engine, tenant="acme",
                            session_key="  Sess-RAW-7 ") == [1]
    assert jev_selector.record_withheld("query_empty", legacy_ids=[1], engine=engine, tenant="acme",
                                        session_key="sess-raw-7")

    keys = [row["session_key"] for row in ledger_rows(tmp_path, "SELECT session_key FROM decisions")]
    assert len(keys) == 3 and set(keys) == {decision_session_key("Sess-RAW-7", "acme")}
    assert decision_session_key("Sess-RAW-7", "acme") != decision_session_key("Sess-RAW-7")
    assert b"sess-raw-7" not in ledger_bytes(tmp_path).lower()


"""T-2271: the five delivery failures of the 2026-11-09 external review, as regression tests.

Every ``xfail(strict=True)`` test below describes the behaviour the product SHOULD have and fails today for the
stated reason; the fix card named in its reason flips it to passing (strict: an unexpected pass fails the run, so
the marker must then be removed).  The non-xfail "control" tests pin the part that already works, so a failing
xfail can only be failing for the reason it states.

Everything is hermetic: a temporary HOME, an ephemeral SQLite per test, hooks rendered from the shipped templates
into tmp dirs, no network, never the live ``memorymaster.db``.
"""
from __future__ import annotations

import io
import json
import runpy
import sys
from pathlib import Path

import pytest
from test_codex_recall_hook import installed as rendered_hooks  # fixture: templates rendered into tmp
from test_hook_transcript_fallback import _rec
from test_hook_transcript_fallback import world as fallback_world  # fixture: ephemeral DB + transcript index

from memorymaster.core import lifecycle
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.recall import transcript_fallback
from memorymaster.recall import transcript_index as ti

TEMPLATES = Path(__file__).resolve().parents[1] / "memorymaster" / "config_templates" / "hooks"
FIRMWARE = "7.18.2"
IMPORTED_FIXTURES = (fallback_world, rendered_hooks)  # referenced, so pytest sees them and ruff does not call the parameters redefinitions


# --- 1. P1-2.1: the best transcript fragment only fills a slot the claims left empty --------------------------


def _firmware_world(fallback_world, competing: list[str]):
    fallback_world.transcripts(("assistant", f"The router firmware version we chose was {FIRMWARE} after the rollback",
                       "2026-08-01T00:00:00Z", fallback_world.cwd))
    return [fallback_world.claim(text) for text in competing]


COMPETING = [
    "Check the router firmware version on each deploy",
    "The router firmware version field must be in the deploy checklist",
]


@pytest.mark.parametrize("competing", [0, 1])
def test_firmware_fragment_survives_zero_or_one_competing_claim(fallback_world, competing) -> None:
    """Control: with a free slot the raw fragment carrying the chosen firmware is packed."""
    made = _firmware_world(fallback_world, COMPETING[:competing])
    text, ids = fallback_world.ask()
    assert ids == made
    assert FIRMWARE in text and "Raw past conversation" in text


@pytest.mark.xfail(strict=True, reason="T-2271/P1-2.1: fragments only fill leftover slots "
                   "(recall/context_hook.py _with_transcript_fallback: slots = max_claims - claims), so two "
                   "number-less claims on the same topic push the fragment with the chosen firmware out")
def test_firmware_fragment_is_not_dropped_by_two_competing_claims(fallback_world) -> None:
    made = _firmware_world(fallback_world, COMPETING)
    text, ids = fallback_world.ask()
    assert sorted(ids) == sorted(made), f"precondition: both competing claims are injected, got {ids}"
    assert FIRMWARE in text, "the fragment stating the chosen firmware lost its slot to claims without the number"


def test_firmware_fragment_is_retried_once_the_competing_claims_were_seen(fallback_world) -> None:
    """Sixth case (T-2275 'seen'): turn 2 of the same session no longer repeats the claims, so the slot is free."""
    _firmware_world(fallback_world, COMPETING)
    _, first = fallback_world.ask(session_id="s-firm")
    assert len(first) == 2
    text, again = fallback_world.ask(session_id="s-firm")
    assert again == [] and FIRMWARE in text  # works, but only on the SECOND prompt: the first one lost it (case above)


# --- hooks rendered from the shipped templates, one tmp project ---------------------------------------------


@pytest.fixture()
def hooked(rendered_hooks, tmp_path, monkeypatch):
    """Rendered hooks (``rendered_hooks``) + ephemeral claims DB at the template's DB_PATH + isolated hook state."""
    from memorymaster.core import hook_log

    for name in ("QDRANT_URL", "MEMORYMASTER_SCOPE_DEFAULT", "MEMORYMASTER_STOP_BLOCKING"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MEMORYMASTER_HOOK_RECALL_REMOTE", "0")  # never try the shared server
    monkeypatch.setenv("MEMORYMASTER_DEFAULT_DB", str(tmp_path / "memorymaster.db"))  # the template sets it itself
    monkeypatch.setenv("MEMORYMASTER_SPOOL_DIR", str(tmp_path / "spool"))
    monkeypatch.setenv("MEMORYMASTER_RECALL_SEEN_DIR", str(tmp_path / "seen"))
    monkeypatch.setenv("MEMORYMASTER_RECALL_HOOK_MAX_CLAIMS", "2")
    monkeypatch.setenv(transcript_fallback.ENABLED_ENV, "1")
    monkeypatch.setenv(ti.INDEX_ENV, str(tmp_path / "idx" / "transcripts.db"))
    monkeypatch.delenv(ti.ROOT_ENV, raising=False)  # the default root: <HOME>/.claude/projects
    monkeypatch.setattr(hook_log, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(hook_log, "_LOG_FILE", tmp_path / "hook.log")
    monkeypatch.setattr(sys, "path", list(sys.path))  # the templates insert their project root
    monkeypatch.chdir(tmp_path)  # ... and chdir into it
    MemoryService(tmp_path / "memorymaster.db", workspace_root=tmp_path).init_db()
    project = tmp_path / "proj"
    project.mkdir()

    class Hooked:
        hooks = rendered_hooks
        cwd = str(project)

        def run(self, name: str, data: dict) -> str:
            """Execute a rendered hook as ``__main__`` with ``data`` on stdin; return what it wrote to stdout."""
            out = io.StringIO()
            monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(data)))
            monkeypatch.setattr(sys, "stdout", out)
            runpy.run_path(str(rendered_hooks / name), run_name="__main__")
            return out.getvalue()

        def context(self, raw: str) -> str:
            return json.loads(raw)["hookSpecificOutput"]["additionalContext"] if raw.strip() else ""

        def claude(self, prompt: str, session: str) -> str:
            return self.context(self.run("memorymaster-recall.py",
                                         {"prompt": prompt, "cwd": self.cwd, "session_id": session}))

        def codex(self, prompt: str, session: str) -> str:
            out = io.StringIO()
            monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"prompt": prompt, "cwd": self.cwd,
                                                                      "session_id": session})))
            ns = runpy.run_path(str(rendered_hooks / "memorymaster-codex-recall.py"))
            monkeypatch.setattr(sys, "stdout", out)
            assert ns["main"]("UserPromptSubmit") == 0
            return self.context(out.getvalue())

    return Hooked()


# --- 2. T-2277: a decision taken in one CLI reaches the other ------------------------------------------------

CLAUDE_DECISION = "pelican strategy"  # stated in a Claude session
CODEX_DECISION = "45 days"  # stated in a Codex session
CLAUDE_ASK = "which strategy did we choose for the zorblax cache invalidation"
CODEX_ASK = "what retention window did we pick for the quartzite audit logs"


def _two_cli_sessions(hooked, tmp_path: Path) -> None:
    claude_dir = tmp_path / ".claude" / "projects" / "p"
    claude_dir.mkdir(parents=True)
    (claude_dir / "claude-session.jsonl").write_text(_rec(
        "assistant", f"We decided the zorblax cache invalidation strategy is the {CLAUDE_DECISION}, final.",
        "2026-11-01T10:00:00Z", hooked.cwd) + "\n", encoding="utf-8")
    codex_dir = tmp_path / ".codex" / "sessions" / "2026" / "11" / "02"
    codex_dir.mkdir(parents=True)

    def line(kind: str, payload: dict) -> str:
        return json.dumps({"timestamp": "2026-11-02T10:00:00Z", "type": kind, "payload": payload})

    codex_lines = [
        line("session_meta", {"id": "0198a1b2-c3d4-4e5f-8a9b-0c1d2e3f4a5b", "cwd": hooked.cwd}),
        line("response_item", {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "What retention window should the quartzite audit logs have?"}]}),
        line("response_item", {"type": "message", "role": "assistant", "content": [
            {"type": "output_text",
             "text": f"We decided the quartzite audit logs retention window is {CODEX_DECISION}, final."}]}),
    ]
    (codex_dir / "rollout-2026-11-02T10-00-00-0198a1b2-c3d4-4e5f-8a9b-0c1d2e3f4a5b.jsonl").write_text(
        "\n".join(codex_lines) + "\n", encoding="utf-8")
    ti.index_transcripts()  # what the scheduled task runs: default root, default sidecar


def test_control_codex_receives_the_decision_taken_in_claude(hooked, tmp_path) -> None:
    _two_cli_sessions(hooked, tmp_path)
    assert CLAUDE_DECISION in hooked.codex(CLAUDE_ASK, "codex-1")


@pytest.mark.xfail(strict=True, reason="T-2277: recall/transcript_index.py indexes only ~/.claude/projects "
                   "(Claude record shape), so a decision stated in a Codex rollout never reaches the Claude hook")
def test_decision_crosses_cli_in_both_directions(hooked, tmp_path) -> None:
    _two_cli_sessions(hooked, tmp_path)
    assert CLAUDE_DECISION in hooked.codex(CLAUDE_ASK, "codex-1"), "precondition: Claude -> Codex works today"
    assert CODEX_DECISION in hooked.claude(CODEX_ASK, "claude-1"), "Codex -> Claude: the Codex decision is missing"


# --- 3. T-2276: a correction written in prose supersedes the claim it corrects -------------------------------


def test_prose_correction_is_not_served_next_to_the_claim_it_corrects(fallback_world, monkeypatch) -> None:
    monkeypatch.setenv(transcript_fallback.ENABLED_ENV, "0")
    ask = "which port does the staging admin panel listen on"
    old = fallback_world.claim("The staging admin panel listens on port 8080")
    # T-2276: the correction must NAME the claim it corrects (the safety net is anchored on an explicit id, never on
    # topic), so the text carries the old claim's human id.
    old_id = fallback_world.service.store.get_claim(old, include_citations=False).human_id
    new = fallback_world.claim(f"Correction: the staging admin panel listens on port 9443; this supersedes {old_id} "
                               "(the earlier claim that it listens on port 8080 was wrong)")
    text, ids = fallback_world.ask(ask)
    assert new in ids, f"precondition: the correction is served, got {ids}"
    old_lines = [ln for ln in text.splitlines() if f"mm-{old:x}" in ln or f"#{old} " in ln or f"mm-{old}]" in ln]
    served_as_current = old in ids and not any("superseded" in ln.lower() for ln in old_lines)
    assert not served_as_current, f"the corrected claim #{old} is still served as current: ids={ids}"


# --- 4. T-2274: the optional Stop hook sends sanitized text to the provider ---------------------------------

MARKERS = {
    "provider_api_key": "sk-ant-api03-" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4",
    "jwt": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    "bearer_header": "Bearer " + "abcdef0123456789abcdef0123456789",
    "db_url_password": "S3cretPass" + "99",
    "base64_secret": "c2VjcmV0LXZhbHVlLWZvci10ZXN0aW5nLTEyMzQ1Njc4OTA=",
    "card_number": "4111 1111 " + "1111 1111",
    "argentine_dni": "DNI 12.345." + "678",
    "private_key_header": "-----BEGIN RSA " + "PRIVATE KEY-----",
    "password_assignment": "password=" + "hunter2hunter2",
}
CARRIER = {
    "db_url_password": "postgres://admin:{}@db.internal.example:5432/app",
    "bearer_header": "Authorization: {}",
}


@pytest.mark.xfail(strict=True, reason="T-2274: _run_gemini_extraction hands the raw assistant text to call_llm; "
                   "nothing sanitizes it before the provider request (redact_text alone also misses the base64 "
                   "blob and the DNI)")
def test_stop_hook_extraction_sends_no_sensitive_marker_to_the_provider(tmp_path, monkeypatch) -> None:
    from memorymaster.core import llm_provider

    project = tmp_path / "project"
    project.mkdir()
    MemoryService(project / "memorymaster.db", workspace_root=project).init_db()
    hook = tmp_path / "stop-hook.py"
    hook.write_text((TEMPLATES / "memorymaster-auto-ingest.py").read_text(encoding="utf-8").replace(
        "__MEMORYMASTER_PROJECT_ROOT__", project.as_posix()), encoding="utf-8")
    transcript = tmp_path / "session.jsonl"
    turns = [{"message": {"role": "user", "content": "please note the deploy details for the rollout"}}]
    for name, marker in MARKERS.items():
        text = CARRIER.get(name, "{}").format(marker)
        turns.append({"message": {"role": "assistant", "content": [
            {"type": "text", "text": f"Noted for the {name} rollout step: {text} (kept in the deploy notes)."}]}})
    transcript.write_text("\n".join(json.dumps(t) for t in turns) + "\n", encoding="utf-8")

    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(tmp_path / "home"))
    for name in ("MEMORYMASTER_STOP_BLOCKING", "MEMORYMASTER_STOP_CAPTURE_VERBATIM", "MEMORYMASTER_STOP_RULE_MINING",
                 "MEMORYMASTER_WAL_DISCIPLINE", "MEMORYMASTER_LLM_FALLBACK_PROVIDER", "MEMORYMASTER_JEV_MODE",
                 "MEMORYMASTER_JEV_RECALL", "MEMORYMASTER_JEV_SESSION"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MEMORYMASTER_CAPTURE_STATE_DB", str(tmp_path / "capture.db"))
    monkeypatch.setenv("MEMORYMASTER_STOP_EXTRACT", "1")
    monkeypatch.setenv("MEMORYMASTER_LLM_PROVIDER", "ollama")
    sent: list[dict] = []

    def provider(url, payload, *args, **kwargs):  # the HTTP client every provider goes through: nothing leaves here
        sent.append(payload)
        return "[]"

    monkeypatch.setattr(llm_provider, "_http_post", provider)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
        "session_id": "stop-e2e", "transcript_path": str(transcript), "cwd": str(project), "stop_hook_active": False})))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    runpy.run_path(str(hook), run_name="__main__")

    assert json.loads(out.getvalue()) == {"decision": "approve"}
    assert sent, "precondition: the extraction ran and called the provider"
    body = "\n".join(str(p.get("prompt", "")) for p in sent)
    leaked = [name for name, marker in MARKERS.items() if marker in body]
    assert leaked == [], f"{len(leaked)} of {len(MARKERS)} sensitive markers reached the provider intact: {leaked}"


# --- 5. T-2273: an internal process does not trigger recall ---------------------------------------------------


@pytest.mark.parametrize("marker", ["MEMORYMASTER_INTERNAL_RUN", "MEMORYMASTER_INTERNAL_CODEX"])
def test_claude_recall_hook_ignores_internal_processes(hooked, monkeypatch, marker) -> None:
    from memorymaster.recall import remote

    calls: list[str] = []

    def ranker(query, data, *, db_path):
        calls.append(query)
        return "\n- [mm-2bff] a governed cited memory", "local"

    monkeypatch.setattr(remote, "hook_recall", ranker)
    monkeypatch.setenv(marker, "1")  # set by dreaming/providers.py for its own `claude -p` children
    out = hooked.run("memorymaster-recall.py", {"prompt": "explain the recall isolation design", "cwd": hooked.cwd,
                                                "session_id": "internal-1"})
    assert calls == [], f"the ranker ran {len(calls)} time(s) for an internal process"
    assert out == ""


def test_control_claude_recall_hook_calls_the_ranker_without_the_internal_mark(hooked, monkeypatch) -> None:
    from memorymaster.recall import remote

    calls: list[str] = []

    def ranker(query, data, *, db_path):
        calls.append(query)
        return "\n- [mm-2bff] a governed cited memory", "local"

    monkeypatch.setattr(remote, "hook_recall", ranker)
    for marker in ("MEMORYMASTER_INTERNAL_RUN", "MEMORYMASTER_INTERNAL_CODEX"):
        monkeypatch.delenv(marker, raising=False)
    out = hooked.run("memorymaster-recall.py", {"prompt": "explain the recall isolation design", "cwd": hooked.cwd,
                                                "session_id": "internal-1"})
    assert len(calls) == 1 and "a governed cited memory" in out


# --- 6 (bonus). T-2275: a claim is marked seen only after final emission -----------------------------------


@pytest.mark.xfail(strict=True, reason="T-2275: context_hook._finish_recall records the injected ids as session-seen "
                   "at render time, before the hook has written them to stdout, so a failed emission loses the "
                   "claims for the rest of the session")
def test_a_claim_whose_emission_failed_is_delivered_on_the_next_prompt(hooked, tmp_path, monkeypatch) -> None:
    svc = MemoryService(tmp_path / "memorymaster.db", workspace_root=tmp_path)
    made = svc.ingest("The zorblax cache invalidation strategy is the pelican strategy",
                      [CitationInput(source="test://seen", locator="x")], scope="project:proj", source_agent="fixture")
    lifecycle.transition_claim(svc.store, made.id, "confirmed", reason="fixture", event_type="validator")
    monkeypatch.setenv(transcript_fallback.ENABLED_ENV, "0")

    class Broken(io.StringIO):
        def write(self, text):  # the pipe to the agent is gone
            raise BrokenPipeError

    assert "pelican strategy" in hooked.claude(CLAUDE_ASK, "other-session"), "precondition: the claim is recalled"
    data = {"prompt": CLAUDE_ASK, "cwd": hooked.cwd, "session_id": "seen-1"}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(data)))
    monkeypatch.setattr(sys, "stdout", Broken())
    runpy.run_path(str(hooked.hooks / "memorymaster-recall.py"), run_name="__main__")  # emission fails, hook swallows
    assert "pelican strategy" in hooked.claude(CLAUDE_ASK, "seen-1"), "the claim was marked seen but never emitted"

